#!/usr/bin/env python3
"""增量摘要生成 v1.0 — 给新入库的记录补 summary（供 collector 调用）

独立脚本, 也可被 collector.py 直接 import 调用。
用 GLM glm-4-flash（非推理模型, 不会把token浪费在思维链上）。
"""
import json
import os
import re
import sqlite3
import time
import urllib.request

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
ENV_PATH = os.environ.get('HERMES_ENV', '/home/ubuntu/.hermes/.env')
BATCH = 10
MAX_CHARS = 700
MODEL = 'glm-4-flash'

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


def _env(k):
    try:
        for line in open(ENV_PATH):
            if line.startswith(k + '='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def call_llm(prompt, timeout=120):
    key = _env('GLMCODE_API_KEY')
    base = _env('GLMCODE_BASE_URL') or 'https://open.bigmodel.cn/api/coding/paas/v4'
    if not key:
        return None
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.2, "max_tokens": 2000}
    req = urllib.request.Request(
        base.rstrip('/') + '/chat/completions', data=json.dumps(payload).encode(),
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
        m = re.match(r'^\[?(\d+)\]?\s*[|｜:：、]?\s*(.+)$', line)
        if not m:
            continue
        idx = int(m.group(1))
        s = m.group(2).strip().strip('|｜').strip()
        if 1 <= idx <= n and 4 <= len(s) <= 200:
            out[idx] = s.rstrip('。.。 ')
    return out


def generate_for(ids=None, limit=50, conn=None):
    """给指定id或未摘要的记录生成摘要。返回生成条数。"""
    own = conn is None
    c = conn or sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=10000')
    try:
        if ids:
            rows = c.execute(
                "SELECT id, content FROM ledger WHERE id IN (%s)"
                % ','.join('?' * len(ids)), tuple(ids)).fetchall()
        else:
            rows = c.execute(
                "SELECT id, content FROM ledger WHERE embed IS NOT NULL "
                "AND (summary IS NULL OR summary='') AND length(content)>=40 "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        if not rows:
            return 0
        done = 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            items = [(rid, content) for rid, content in chunk]
            prompt = PROMPT + '\n\n' + '\n\n'.join(
                '[%d] %s' % (j, re.sub(r'\s+', ' ', strip_system_prefix(t) or t)[:MAX_CHARS])
                for j, (_, t) in enumerate(items, 1))
            try:
                resp = call_llm(prompt)
                if not resp:
                    break
                text = resp['choices'][0]['message']['content']
                sums = parse_summaries(text, len(items))
            except Exception:
                continue
            if not sums:
                continue
            c.executemany('UPDATE ledger SET summary=? WHERE id=?',
                          [(s, items[idx - 1][0]) for idx, s in sums.items()])
            c.commit()
            done += len(sums)
        return done
    finally:
        if own:
            c.close()


if __name__ == '__main__':
    n = generate_for(limit=int(sys.argv[1]) if len(sys.argv) > 1 else 50)
    print('generated %d summaries' % n)
