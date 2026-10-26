#!/usr/bin/env python3
"""会话档案生成 v1.0 — 每天一篇 Markdown 档案(OpenViking式目录归档)

用途: 把当天的会话整理成可翻阅的档案文件, 按日期/主题分目录存放。
原文永远在 ledger 里, 这只是给人看的浓缩视图, 不影响检索。
"""
import os
import re
import sqlite3
import sys
import time

LEDGER = '/opt/memory-ledger/ledger.db'
ARCHIVE_DIR = '/opt/memory-ledger/archives'
NOISE = re.compile(
    r'(用sheet-mcp读取在线进度表|scheduled cron job|ASYNC DELEGATION|'
    r'OUT-OF-BAND USER MESSAGE|Background process|'
    r'Gateway message origin|This is an automated|'
    r'你是资深|方案负责人|独立审核一份)', re.S)


def strip_prefix(t):
    for pat in (r'^\[IMPORTANT[^\]]*\]\s*', r'^\[System[^\]]*\]\s*',
                r'^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*'):
        t = re.sub(pat, '', t, flags=re.S)
    return re.sub(r'\[IMPORTANT[^\]]*\]', '', t).strip()


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else time.strftime('%Y-%m-%d')
    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=10000')
    rows = c.execute(
        "SELECT ts, session_id, role, content, type FROM ledger "
        "WHERE role IN ('user','assistant') AND embed IS NOT NULL "
        "ORDER BY ts").fetchall()

    out_dir = os.path.join(ARCHIVE_DIR, day)
    os.makedirs(out_dir, exist_ok=True)

    by_sess = {}
    for ts, sess, role, content, typ in rows:
        d = time.strftime('%Y-%m-%d', time.localtime(ts))
        if d != day:
            continue
        txt = strip_prefix(content or '')
        if not txt or len(txt) < 10:
            continue
        if NOISE.search(content or ''):
            continue
        by_sess.setdefault(sess, []).append((ts, role, txt, typ))

    if not by_sess:
        print('no sessions for', day)
        return

    lines = ['# %s 会话档案' % day, '']
    lines.append('- 归档时间: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('- 会话数: %d' % len(by_sess))
    lines.append('- 记录数: %d' % sum(len(v) for v in by_sess.values()))
    lines.append('')

    for sess, items in sorted(by_sess.items(), key=lambda kv: kv[1][0][0]):
        lines.append('## 会话 %s' % sess[:18])
        lines.append('')
        # 只保留 user 提问和 assistant 结论的前段, tool 已经在库里
        for ts, role, txt, typ in items:
            if role == 'user':
                lines.append('- **你**：%s' % txt.replace('\n', ' ')[:300])
            else:
                head = txt.replace('\n', ' ')[:220]
                lines.append('  - 助手[%s]：%s' % (typ or 'fact', head))
        lines.append('')

    path = os.path.join(out_dir, 'session-archive.md')
    with open(path, 'w') as f:
        f.write('\n'.join(lines))
    print('written %s (%d bytes, %d sessions)' % (path, os.path.getsize(path), len(by_sess)))
    c.close()


if __name__ == '__main__':
    main()
