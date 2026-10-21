#!/usr/bin/env python3
"""记忆账本检索 v1.3: 三链查询(FTS jieba/trigram/LIKE)+snippet+量化注入"""
import sqlite3, sys, json, time, argparse, re, os
import jieba

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
JIEBA_DICT = LEDGER_DIR + '/user_dict.txt'
TOP_K = 5
BUDGET = 2000
HALF_LIFE = {'fact': 90 * 86400, 'preference': 90 * 86400, 'state': 7 * 86400}

if os.path.exists(JIEBA_DICT):
    jieba.load_userdict(JIEBA_DICT)

def connect():
    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=5000')
    return c

def tokenize(text):
    return ' '.join(w for w in jieba.cut_for_search(text) if w.strip())

def search(keyword, top_k=TOP_K, days=None, session=None, role=None):
    c = connect()
    now = time.time()
    results = []
    toks = tokenize(keyword)
    try:
        rows = c.execute("SELECT rowid FROM fts_jieba WHERE fts_jieba MATCH ? LIMIT ?",
                         (toks, top_k * 4)).fetchall()
        results += [r[0] for r in rows]
    except Exception:
        pass
    if len(keyword) >= 3:
        try:
            rows = c.execute("SELECT rowid FROM fts_trigram WHERE fts_trigram MATCH ? LIMIT ?",
                             ('"' + keyword + '"', top_k * 4)).fetchall()
            results += [r[0] for r in rows if r[0] not in results]
        except Exception:
            pass
    if len(keyword) <= 2 or not results:
        rows = c.execute("SELECT id FROM ledger WHERE content LIKE ? AND status='active' LIMIT ?",
                         ('%' + keyword + '%', top_k * 4)).fetchall()
        results += [r[0] for r in rows if r[0] not in results]
    out = []
    for rid in results[: top_k * 3]:
        r = c.execute('SELECT ts,session_id,role,content,type,status,source_type,trust_level,origin '
                      'FROM ledger WHERE id=?', (rid,)).fetchone()
        if not r:
            continue
        ts, sess, row_role, content, typ, status, stype, tl, origin = r
        if status != 'active':
            continue
        if days and now - ts > days * 86400:
            continue
        if session and sess != session:
            continue
        if role and row_role not in role:
            continue
        hl = HALF_LIFE.get(typ, 30 * 86400)
        decay = 0.5 ** ((now - ts) / hl)
        trust_b = {'high': 1.0, 'medium': 0.7, 'low': 0.4}.get(tl, 0.5)
        score = decay * trust_b
        m = re.search(re.escape(keyword), content)
        if m:
            st = max(0, m.start() - 100)
            snip = content[st:st + 400]
        else:
            snip = content[:400]
        out.append((score, rid, ts, sess, row_role, snip, stype, tl, origin))
    out.sort(key=lambda x: -x[0])
    sess_count = {}
    final = []
    for o in out:
        if sess_count.get(o[3], 0) >= 2:
            continue
        sess_count[o[3]] = sess_count.get(o[3], 0) + 1
        final.append(o)
        if len(final) >= top_k:
            break
    c.close()
    return final

def format_out(final, keyword):
    total = 0
    body = []
    for score, rid, ts, sess, row_role, snip, stype, tl, origin in final:
        tstr = time.strftime('%m-%d %H:%M', time.localtime(ts))
        block = '[%s|%s|%s/%s] ...%s... (id:%s src:%s)' % (tstr, row_role, stype, tl, snip, rid, origin)
        if total + len(block) > BUDGET:
            break
        total += len(block)
        body.append(block)
    head = '【历史存档·记忆账本】查询"%s" 命中%d条（以MEMORY.md为准）' % (keyword, len(final))
    return head + '\n' + '\n\n'.join(body)

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('keyword')
    ap.add_argument('--days', type=int, default=None)
    ap.add_argument('--session', default=None)
    ap.add_argument('--top', type=int, default=TOP_K)
    ap.add_argument('--role', default=None, help='filter by role: user/assistant/tool, comma-sep')
    a = ap.parse_args()
    roles = [r.strip() for r in a.role.split(',')] if a.role else None
    final = search(a.keyword, a.top, a.days, a.session, role=roles)
    print(format_out(final, a.keyword))
