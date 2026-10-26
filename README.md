# memory-ledger 「小账本」

给 AI Agent 加一个**外挂记忆账本**：把历史会话全文索引进 SQLite，**关键词 + 本地向量双路召回**，Agent 忘了什么随查随得——上下文压缩不再是"失忆"，而是"延迟检索"。

**零 API 依赖**：嵌入模型跑在本地，不花钱、不联网、不上传数据。

## 为什么需要它

长对话 AI Agent 的普遍痛点：上下文窗口有限，压缩/截断后**过程细节永久丢失**——上次修的 bug 参数、三天前用户拍板的结论、某次报错的完整堆栈。

- `MEMORY.md` / memory 工具 = 精华结论（Agent 主动记，会丢细节）
- **小账本** = 行车记录仪（全量存档，忘了就回放查）

两者互补。设计目标：**零 API 依赖、常驻成本趋近零、坏了自己会喊**。

## 特性

- **双路召回**：① 关键词路（jieba FTS → trigram FTS → LIKE 三链，从准到宽逐级降级）② 语义路（本地 bge-small-zh-v1.5，512维向量，numpy 暴力检索）——**只记得大概的问法也能查**
- **本地嵌入模型**：`BAAI/bge-small-zh-v1.5`（92MB，torch 本地推理）。不调 embedding API、永久成本为零、数据不出本机；7293 条检索 2ms，不需要向量数据库
- **记忆分层**：fact / preference / state / procedure / noise，半衰期按类型独立（状态7天、偏好90天、流程10年）
- **会话档案层**：每天一篇 Markdown 归档，可直接翻阅；原文永远在库里，档案只是浓缩视图
- **摘要层 L0**：LLM 生成 30-50 字浓缩摘要（必须含实体：日期/单号/服务名/数字），查询时优先展示摘要而非全文 → 输出量降一个数量级
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
$ query.py '项目A' --days 7           # 只看近7天
$ query.py '项目A' --no-semantic      # 只走关键词（快，省模型加载）
```

**双路召回实测**（7 道真实模糊问法，关键词路 vs 双路）：

| 问法 | 关键词路 | 双路 |
|---|---|---|
| 「摄像头画面全白是咋回事」 | ❌ 0条 | ✅ 命中 |
| 「你怎么处理同事改的垃圾数量的」 | ❌ 0条 | ✅ 命中 |
| 「那个CAD导出坐标的插件怎么用」 | ✅ 1条 | ✅ 5条 |

6087 条 / 46MB 库：FTS 链 0.7ms，LIKE 兜底 15ms，向量检索 2ms，词典加载 0.6s 为固定开销——**撑到十万条级无感知**。

## 语义层怎么用

首次查询会加载模型（约 2-3 秒），之后进程内缓存。模型自动从 HuggingFace 缓存读取（国内走 `HF_ENDPOINT=https://hf-mirror.com`）。

```bash
# 全量回填向量（已有编码的跳过，断点续传）
python3 embed_backfill.py
python3 embed_backfill.py --all      # 强制重编全部

# 重新打记忆分层标签
python3 classify_backfill.py

# 生成某天的会话档案
python3 make_archive.py 2026-10-26

# 生成摘要（L0层）
python3 make_summary.py          # 全量回填
python3 summary_inc.py 50        # 增量补前50条
```

**摘要生成的两个坑**（都会让摘要变成废话）：
1. **必须先剥 `[IMPORTANT: ...]` 系统注入前缀**，否则 LLM 看到的是"作为计划任务运行"而不是真内容
2. **用 `glm-4-flash` 不要用 `glm-4.6`**：4.6 是推理模型，1194 个 reasoning_tokens 会把正文挤掉，`finish_reason=length` 导致解析率归零

**为什么本地嵌入而不是调 API**：
- API 版（如 MiniMax embo-01）也可用，但**永久成本** + 依赖网络/密钥 + 数据要出境
- 实测中文历史场景 bge 更准：「那个CAD导出坐标的插件怎么用」bge 0.701 vs embo-01 0.440
- 本地 20 条/秒（4核CPU），7293 条全量编码 6 分钟

**哪些记录做向量化**：只做 `user`/`assistant` 角色。`tool` 记录占 81% 且是执行噪音，跳过（省80%成本且不损失检索能力）。

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
#    要语义层还需 torch + transformers（见下）
crontab -e
*/5 * * * * cd /opt/memory-ledger && python3 collector.py >> collector.log 2>&1
0 */6 * * * /opt/memory-ledger/snapshot.sh
35 * * * * /opt/memory-ledger/selfcheck.sh

# 4) 验证
python3 /opt/memory-ledger/query.py '测试'
tail /opt/memory-ledger/collector.log   # 看到 inserted=N 即通
```

### 启用语义层（可选，但推荐）

```bash
pip install torch transformers        # CPU 版即可
export HF_ENDPOINT=https://hf-mirror.com   # 国内加速
python3 embed_backfill.py            # 回填历史向量
```

不用语义层的话，keyword 路依然完全可用（`--no-semantic` 或不装 torch 都能跑）。

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
| `query.py` | **双路召回**（关键词三链+本地向量）+打分排序+角色过滤+量化注入（≤2000字符预算） |
| `embed_backfill.py` | 向量回填（剥系统注入前缀→去重→本地编码→float16存储，断点续传） |
| `classify_backfill.py` | 记忆分层打标（轻量正则，不用LLM，零成本） |
| `make_archive.py` | 会话档案生成（每日 Markdown，可翻阅） |
| `make_summary.py` | 摘要层回填（剥系统前缀→LLM提炼30-50字含实体摘要） |
| `summary_inc.py` | 摘要增量补（collector 调用，也可独立跑） |
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
- 向量路只覆盖 `user`/`assistant` 记录（`tool` 记录是噪音，不向量化）
- 首次查询加载模型约 2-3 秒；无网络且模型未缓存时向量路失败，会自动降级为纯关键词
- 回声降权：assistant 消息里逐字复述了整条查询会被降权 0.35 倍（防自己刚写的复述稿霸榜）
- 告警防轰炸：每小时最多一条

## License

MIT
## 性能：常驻向量服务（v2.2）

每次查询重新加载嵌入模型（92MB）会让冷启动耗时 **8.4 秒**。本项目提供常驻服务解决：

```bash
# 启动（模型预加载约7秒）
python3 vector_server.py          # 监听 127.0.0.1:9137

# systemd 托管（推荐）
cp vector_server.py /opt/memory-ledger/
# 参考 README 的 systemd 段落配置 memory-ledger-vector.service
```

接口：
```bash
curl http://127.0.0.1:9137/health
curl -X POST http://127.0.0.1:9137/encode -d '{"texts":["测试"],"prefix":"..."}'
curl -X POST http://127.0.0.1:9137/search -d '{"query":"模糊问法","top":20}'
```

`query.py` 会优先走服务，**服务不可用时自动降级为本地加载**（功能不受影响，只是慢）。
实测：查询耗时 **8.6 秒 → 0.93 秒**，常驻内存约 332MB。
