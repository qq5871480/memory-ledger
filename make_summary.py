#!/usr/bin/env python3
"""摘要层生成 v1.0 — 给 ledger 记录生成 L0 摘要（50字内，含实体词）

用途: 查询时先扫摘要（20条×50字=1000字），命中才拉全文 → 省一个数量级的token。
约束: 摘要必须包含具体实体（地址/行号/单号/人名/日期），否则等于检索废话。

用 GLM API（批量，一次请求多条），失败重试。
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
BATCH = 10
MAX_CHARS = 700

# 从 .env 读 key
ENV_PATH = os.environ.get('HERMES_ENV', '/home/ubuntu/.hermes/.env')


def get_key():
    try:
        for line in open(ENV_PATH):
            if line.startswith('GLMCODE_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def get_base():
    try:
        for line in open(ENV_PATH):
            if line.startswith('GLMCODE_BASE_URL='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return 'https://open.bigmodel.cn/api/coding/paas/v4'


PROMPT = """你是记忆摘要器。给每条记录写一句不超过40字的中文摘要。

硬要求（违反任何一条都算不合格）：
1. **必须包含具体实体**：日期(9月22日/2026-10-24)、小区名或地址、文件名单号、行号、模型名、技能名、服务名、具体数字
2. **长度30-50字**，太短说明没抓到实体
3. 只写这条记录"讲了什么结论/发生了什么"，不写过程
4. 禁止用"作为计划任务运行""自动响应""执行脚本"这类没有信息量的概括
5. 如果内容本身是系统模板（cron/技能调用），就写清是哪个技能/哪个任务

输出格式：每行一条 `编号|摘要`，不要任何额外说明。"""


def strip_system_prefix(t):
    """剥掉 [IMPORTANT: ...] 等系统注入前缀, 否则LLM看到的是模板而不是真内容"""
    for pat in (r'^\[IMPORTANT[^\]]*\]\s*', r'^\[System[^\]]*\]\s*',
                r'^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*'):
        t = re.sub(pat, '', t, flags=re.S)
    return re.sub(r'\[IMPORTANT[^\]]*\]', '', t).strip()


def build_prompt(items):
    lines = []
    for i, (rid, content) in enumerate(items, 1):
        txt = strip_system_prefix(content)
        if len(txt) < 20:
            txt = re.sub(r'\s+', ' ', content)[:MAX_CHARS]
        else:
            txt = re.sub(r'\s+', ' ', txt)[:MAX_CHARS]
        lines.append('[%d] %s' % (i, txt))
    return PROMPT + '\n\n' + '\n\n'.join(lines)


def call_llm(prompt, key, base, timeout=120):
    url = base.rstrip('/') + '/chat/completions'
    # glm-4-flash: 非推理模型, 不会把token浪费在思维链上
    # (glm-4.6 是推理模型, 1194 reasoning_tokens 会把内容挤掉, finish_reason=length)
    payload = {
        "model": "glm-4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_summaries(text, n):
    out = {}
    if not text:
        return out
    for line in text.split('\n'):
        line = line.strip().lstrip('-*· ').strip()
        if not line:
            continue
        # 兼容 "[1] 摘要" / "[1]|摘要" / "[1]｜摘要" / "1|摘要" / "1：摘要"
        m = re.match(r'^\[?(\d+)\]?\s*[|｜:：、]?\s*(.+)$', line)
        if not m:
            continue
        idx = int(m.group(1))
        s = m.group(2).strip().strip('|｜').strip()
        if 1 <= idx <= n and 4 <= len(s) <= 200:
            out[idx] = s.rstrip('。.。 ')
    return out


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    key = get_key()
    if not key:
        print('[FAIL] 找不到 GLMCODE_API_KEY')
        return
    base = get_base()

    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=10000')
    sql = ("SELECT id, content FROM ledger "
           "WHERE embed IS NOT NULL AND (summary IS NULL OR summary='') "
           "AND length(content) >= 40 ORDER BY id")
    rows = c.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]
    print('待生成摘要: %d 条' % len(rows), flush=True)
    if not rows:
        return

    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        items = [(rid, content) for rid, content in chunk]
        try:
            resp = call_llm(build_prompt(items), key, base)
            text = resp['choices'][0]['message']['content']
            sums = parse_summaries(text, len(items))
        except Exception as e:
            print('  批次 %d 失败: %s' % (i, e), flush=True)
            continue
        if not sums:
            print('  批次 %d 解析失败, 原文前120字: %s'
                  % (i, str(text)[:120] if 'text' in dir() else ''), flush=True)
            continue
        upd = []
        for idx, s in sums.items():
            rid = items[idx - 1][0]
            upd.append((s, rid))
        c.executemany('UPDATE ledger SET summary=? WHERE id=?', upd)
        c.commit()
        done += len(upd)
        print('  %d/%d 已生成 (本批%d条) 用时%.1fs'
              % (done, len(rows), len(upd), time.time() - t0), flush=True)

    n = c.execute("SELECT COUNT(*) FROM ledger WHERE summary IS NOT NULL AND summary!=''").fetchone()[0]
    print('完成: 共 %d 条有摘要' % n)
    c.close()


if __name__ == '__main__':
    t0 = time.time()
    main()
