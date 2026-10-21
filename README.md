# memory-ledger 「小账本」

给 AI Agent 加一个**外挂记忆账本**：把历史会话全文索引进 SQLite（FTS5，jieba+trigram 双索引），Agent 忘了什么随查随得——上下文压缩不再是"失忆"，而是"延迟检索"。

## 为什么需要它

长对话 AI Agent 的普遍痛点：上下文窗口有限，压缩/截断后**过程细节永久丢失**——上次修的 bug 参数、三天前用户拍板的结论、某次报错的完整堆栈。

- `MEMORY.md` / memory 工具 = 精华结论（Agent 主动记，会丢细节）
- **小账本** = 行车记录仪（全量存档，忘了就回放查）

两者互补。设计目标：**零 API 依赖、常驻成本趋近零、坏了自己会喊**。

## 特性

- **三链检索**：jieba 分词 FTS → trigram FTS → LIKE 兜底，从准到宽逐级降级，几乎不空手
- **价值排序**：半衰期衰减（事实90天/状态7天）× 可信度加权（agent 1.0 / user 0.7 / 外部转发 0.4），旧事实仍能浮上来，过期状态自动沉底
- **同会话限流**：单个 session 最多返回2条，防止单日刷屏
- **敏感双轨**：高敏（密钥/密码模式）直接拒收；低敏（手机号/身份证/银行卡）自动脱敏
- **自检告警**：40分钟未入库告警、24h断流升级、每小时限1条防轰炸；webhook 可插拔
- **一致性快照**：`.backup` 在线快照（禁直拷活库）+ SHA256 校验 + 恢复 runbook
- **tool 输出降噪**：>500字工具回包只存头尾（1022优化），检索信噪比翻倍
- **角色过滤**：`--role assistant` 只看人话结论，滤掉工具回包

## 检索效果（实测）

```
$ query.py '水位'
【历史存档·记忆账本】查询"水位" 命中5条（以MEMORY.md为准）
[10-14 22:34|assistant|agent/medium] ...12条明细→10张单，10/10提交成功...
   （半衰期排序后，结论浮在最上，工具回包沉底）

$ query.py '水位' --role assistant   # 只要人话
$ query.py '9048' --days 7           # 只看近7天
```

6087 条 / 46MB 库：FTS 链 0.7ms，LIKE 兜底 15ms，词典加载 0.6s 为固定开销——**撑到十万条级无感知**。

## 快速部署

```bash
git clone https://github.com/qq5871480/memory-ledger.git
cd memory-ledger
pip install jieba          # 唯一三方依赖

# 1) 初始化目录（默认 /opt/memory-ledger，可用 LEDGER_DIR 覆盖）
sudo mkdir -p /opt/memory-ledger && sudo chown $USER /opt/memory-ledger
cp *.py *.sh user_dict.txt /opt/memory-ledger/

# 2) 配采集源（默认读 Hermes 的 state.db，其他 Agent 改这一处 SQL 即可）
#    collector.py: HERMES_DB 环境变量或改默认值
export HERMES_DB=~/.hermes/state.db

# 3) 装三条 cron（注意用装了 jieba 的 python！系统 python 缺包会静默空转）
crontab -e
*/5 * * * * cd /opt/memory-ledger && python3 collector.py >> collector.log 2>&1
0 */6 * * * /opt/memory-ledger/snapshot.sh
35 * * * * /opt/memory-ledger/selfcheck.sh

# 4) 验证
python3 /opt/memory-ledger/query.py '测试'
tail /opt/memory-ledger/collector.log   # 看到 inserted=N 即通
```

### 适配其他 Agent 框架

只改 `collector.py` 一处：把 SQL 换成你自己会话库的（需要字段：`id, session_id, role, content, timestamp`），其余全部复用。角色名映射在 `source_type()`。

## 接入 Agent 的关键一步

**装完不接等于没装**——账本的价值在"Agent 忘了时知道去查"。两种接法：

1. **压缩恢复提示**（Hermes 用户）：`apply_patch.py` 把查询指针注入 Hermes 压缩恢复 footer，升级后跑 `reapply_compaction_patch.sh` 重打
2. **系统提示**（其他框架）：在 system prompt 或记忆文件里加一条硬规矩：

> 长任务复盘、细节对不上、历史结论查不到时，先查小账本（`python3 /opt/memory-ledger/query.py '关键词'`）再回答，不许凭印象编。

## 文件清单

| 文件 | 干什么 |
|---|---|
| `collector.py` | 增量采集：读会话库→脱敏→分词→入账（幂等，src_msg_id 防重） |
| `query.py` | 三链检索+打分排序+角色过滤+量化注入（≤2000字符预算） |
| `snapshot.sh` | 6小时一致性快照+SHA256+NAS同步（可选）+滚动清理 |
| `selfcheck.sh` | 每小时自检：断流告警（40min/24h 两级，每小时限1条） |
| `push_alert.py` | 告警推送（默认打日志，配 `ALERT_WEBHOOK_URL` 走 webhook） |
| `restore.sh` | 恢复 runbook（八步，RTO≤1h） |
| `apply_patch.py` | Hermes 压缩恢复提示注入补丁（幂等，锚文本失效显式报错） |
| `user_dict.txt` | jieba 个人词典（示例为建筑行业词条，按自己领域改） |

## 架构红线

- 账本=**过程档案只增不改**；`MEMORY.md`=现行结论，两者冲突以后者为准
- 数据源**只读挂载**（`mode=ro`），collector 永不写源库
- 检索结果一律标【历史存档】——是"当时说过"，不是"现在有效"；状态型条目 `valid_to` 过期自动降权
- **cron 必须用装了 jieba 的解释器绝对路径**（血泪教训：系统 python 缺包静默空转2小时才被抓）

## 已知边界

- "压缩先于采集"窗口期内容会丢（采集间隔内的最后一次压缩）
- 纯关键词无语义泛化：查"钢筋报审"召不回"材料复试"——向量检索评估过，暂不上（利弊分析见 discussion）
- 告警防轰炸：每小时最多一条

## License

MIT
