#!/usr/bin/env python3
"""记忆账本采集器 v1.3段1-r2: 数据源=Hermes state.db messages表"""
import sqlite3, json, os, re, sys, time
import jieba

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
STATE = LEDGER_DIR + '/collector_state.json'
HERMES_DB = os.environ.get('HERMES_DB', os.path.expanduser('~/.hermes/state.db'))
JIEBA_DICT = LEDGER_DIR + '/user_dict.txt'

if os.path.exists(JIEBA_DICT):
    jieba.load_userdict(JIEBA_DICT)

HIGH_SENS = [
    re.compile(r'(sk-[a-zA-Z0-9]{20,})'),
    re.compile(r'(appsecret|app_secret|api[_-]?key|token)["\'=:\s]+[a-zA-Z0-9]{16,}', re.I),
    re.compile(r'BEGIN (RSA |EC )?PRIVATE KEY'),
    re.compile(r'(password|passwd|pwd)["\'=:\s]+\S{6,}', re.I),
]
LOW_SENS = [
    (re.compile(r'\b(\d{6})(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'), '\1***********'),
    (re.compile(r'\b(1[3-9]\d)\d{4}(\d{4})\b'), '\1****\2'),
    (re.compile(r'\b(\d{6})\d{9}(\d{4})\b'), '\1*********\2'),
]

def sanitize(text):
    had_high = any(p.search(text) for p in HIGH_SENS)
    had_low = False
    for p, rep in LOW_SENS:
        if p.search(text):
            had_low = True
            text = p.sub(rep, text)
    return text, had_high, had_low

def source_type(role, text):
    if role in ('user', 'human'):
        base, tl = 'user', 'medium'
    elif role in ('assistant', 'ai'):
        base, tl = 'agent', 'medium'
    else:
        base, tl = 'system', 'low'
    if re.search(r'\[(?:微信|weixin|转发|forward)', text, re.I):
        return 'external_forward', 'low'
    return base, tl

def tokenize(text):
    return ' '.join(w for w in jieba.cut_for_search(text) if w.strip())

SQL_INIT = """
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY,
  ts INTEGER, session_id TEXT, role TEXT,
  content TEXT, jieba_tokens TEXT, tags TEXT,
  type TEXT DEFAULT 'fact', status TEXT DEFAULT 'active',
  confidence REAL DEFAULT 0.8,
  valid_from INTEGER, valid_to INTEGER, supersedes INTEGER,
  source_type TEXT, trust_level TEXT, origin TEXT,
  src_msg_id INTEGER UNIQUE
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_trigram USING fts5(content, tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_jieba USING fts5(tokens, content='', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

def init_db():
    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=5000')
    c.executescript(SQL_INIT)
    return c

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'last_msg_id': 0}

def save_state(s):
    json.dump(s, open(STATE, 'w'))

def main():
    c = init_db()
    h = sqlite3.connect('file:' + HERMES_DB + '?mode=ro', uri=True)  # 只读挂源库
    st = load_state()
    last_id = st.get('last_msg_id', 0)
    inserted, skipped_high = 0, 0
    now = int(time.time())
    rows = h.execute(
        'SELECT id, session_id, role, content, timestamp FROM messages '
        'WHERE id > ? AND active=1 AND content IS NOT NULL AND length(content)>=15 '
        'ORDER BY id LIMIT 2000', (last_id,)).fetchall()
    sid_cache = {}
    for mid, sess, role, text, ts in rows:
        st['last_msg_id'] = mid
        if not text or len(text) < 15:
            continue
        if len(text) > 4000:
            text = text[:4000]
        clean, had_high, had_low = sanitize(text)
        if had_high:
            skipped_high += 1
            continue
        # tool输出降噪（1022）：>500字只留头200+尾100+省略标记，检索信噪比优先
        if role == 'tool' and len(clean) > 500:
            clean = clean[:200] + '\n...[中间省略' + str(len(clean)-300) + '字]...\n' + clean[-100:]
        stype, tl = source_type(role, clean)
        toks = tokenize(clean[:1500])
        cur = c.execute(
            'INSERT OR IGNORE INTO ledger(ts,session_id,role,content,jieba_tokens,tags,'
            'type,status,confidence,valid_from,valid_to,source_type,trust_level,origin,src_msg_id) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (int(ts or now), sess, role, clean, toks, '', 'fact', 'active', 0.8,
             int(ts or now), None, stype, tl, 'hermes:msg'+str(mid), mid))
        if cur.rowcount:
            rowid = cur.lastrowid
            c.execute('INSERT INTO fts_trigram(rowid, content) VALUES(?,?)', (rowid, clean))
            c.execute('INSERT INTO fts_jieba(rowid, tokens) VALUES(?,?)', (rowid, toks))
            inserted += 1
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_run',?)", (str(now),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_inserted',?)", (str(inserted),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_msg_id',?)", (str(st['last_msg_id']),))
    c.commit(); c.close(); h.close()
    save_state(st)
    print('inserted=%d skipped_high=%d last_id=%d' % (inserted, skipped_high, st['last_msg_id']))

if __name__ == '__main__':
    main()
