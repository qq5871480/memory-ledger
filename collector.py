#!/usr/bin/env python3
"""记忆账本采集器 v2.0: 数据源=Hermes state.db messages表 + 增量向量化

v2.0 变更:
  - 新增 summary 字段(LLM提炼摘要, 含实体词, 供分层快速扫描)
  - 新增 embed 字段(本地 bge-small-zh-v1.5 向量, 512维 float16)
  - 新增 mem_type 字段(fact/preference/state/procedure/experience)
  - 增量向量: 新插入的 user/assistant 记录同步编码, 不阻塞采集
"""
import sqlite3, json, os, re, sys, time
import jieba

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
STATE = LEDGER_DIR + '/collector_state.json'
HERMES_DB = os.environ.get('HERMES_DB', '/home/ubuntu/.hermes/state.db')
JIEBA_DICT = LEDGER_DIR + '/user_dict.txt'
EMBED_MODEL = 'bge-small-zh-v1.5'

if os.path.exists(JIEBA_DICT):
    jieba.load_userdict(JIEBA_DICT)

HIGH_SENS = [
    re.compile(r'(sk-[a-zA-Z0-9]{20,})'),
    re.compile(r'(appsecret|app_secret|api[_-]?key|token)["\'=:\s]+[a-zA-Z0-9]{16,}', re.I),
    re.compile(r'BEGIN (RSA |EC )?PRIVATE KEY'),
    re.compile(r'(password|passwd|pwd)["\'=:\s]+\S{6,}', re.I),
]
LOW_SENS = [
    (re.compile(r'\b(\d{6})(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'), '\\1***********'),
    (re.compile(r'\b(1[3-9]\d)\d{4}(\d{4})\b'), '\\1****\\2'),
    (re.compile(r'\b(\d{6})\d{9}(\d{4})\b'), '\\1*********\\2'),
]

# 记忆分层判定(轻量规则, 不用LLM, 避免每次采集都花钱)
PROC_MARKERS = re.compile(
    r'(步骤|流程|先.{0,6}再|执行顺序|操作步骤|步骤[一二三四五六1-6]|按以下步骤)', re.S)
PREF_MARKERS = re.compile(
    r'(以后|从今以后|以后都|不要|别再|必须|记住|每次都|优先|偏好|习惯|规则是|口径是)', re.S)
STATE_MARKERS = re.compile(
    r'(当前|现在|目前|正在|今天|本周|截至|暂时|已提交|进行中|水位)', re.S)


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


def classify(content, role):
    """轻量分层: procedure > preference > state > fact"""
    if role in ('user', 'human'):
        if PREF_MARKERS.search(content):
            return 'preference'
        if PROC_MARKERS.search(content):
            return 'procedure'
    if STATE_MARKERS.search(content) and len(content) < 400:
        return 'state'
    return 'fact'


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
    c.execute('PRAGMA busy_timeout=10000')
    c.executescript(SQL_INIT)
    # v2.0 增量列(幂等)
    for col, ddl in [('summary', 'TEXT'), ('embed', 'BLOB'),
                     ('embed_model', 'TEXT'), ('mem_type', "TEXT DEFAULT 'fact'")]:
        try:
            c.execute('ALTER TABLE ledger ADD COLUMN %s %s' % (col, ddl))
        except sqlite3.OperationalError:
            pass
    return c


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'last_msg_id': 0}


def save_state(s):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def strip_system_prefix(text):
    """剥离 [IMPORTANT: ...] 等系统注入前缀, 避免模板噪音"""
    for pat in (r'^\[IMPORTANT[^\]]*\]\s*', r'^\[System[^\]]*\]\s*',
                r'^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*'):
        text = re.sub(pat, '', text, flags=re.S)
    return re.sub(r'\[IMPORTANT[^\]]*\]', '', text).strip()


_encoder = {}


def get_encoder():
    if 'e' in _encoder:
        return _encoder['e']
    try:
        os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
        import torch
        from transformers import AutoTokenizer, AutoModel
        torch.set_num_threads(max(1, (os.cpu_count() or 4)))
        tok = AutoTokenizer.from_pretrained('BAAI/' + EMBED_MODEL)
        mdl = AutoModel.from_pretrained('BAAI/' + EMBED_MODEL)
        mdl.eval()
        _encoder['e'] = (tok, mdl)
    except Exception as e:
        sys.stderr.write('encoder unavailable: %s\n' % e)
        _encoder['e'] = (None, None)
    return _encoder['e']


def encode_texts(texts):
    tok, mdl = get_encoder()
    if tok is None:
        return None
    import numpy as np
    import torch
    b = tok(texts, padding=True, truncation=True, max_length=512, return_tensors='pt')
    with torch.no_grad():
        o = mdl(**b).last_hidden_state[:, 0]
    v = o.numpy().astype('float32')
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v.astype('float16')


def main():
    c = init_db()
    h = sqlite3.connect('file:' + HERMES_DB + '?mode=ro', uri=True)  # 只读挂源库
    st = load_state()
    last_id = st.get('last_msg_id', 0)
    inserted, skipped_high, vec_added = 0, 0, 0
    now = int(time.time())
    rows = h.execute(
        'SELECT id, session_id, role, content, timestamp FROM messages '
        'WHERE id > ? AND active=1 AND content IS NOT NULL AND length(content)>=15 '
        'ORDER BY id LIMIT 2000', (last_id,)).fetchall()
    new_ids = []
    new_texts = []
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
        mtype = classify(clean, role)
        cur = c.execute(
            'INSERT OR IGNORE INTO ledger(ts,session_id,role,content,jieba_tokens,tags,'
            'type,status,confidence,valid_from,valid_to,source_type,trust_level,origin,src_msg_id,mem_type) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (int(ts or now), sess, role, clean, toks, '', mtype, 'active', 0.8,
             int(ts or now), None, stype, tl, 'hermes:msg' + str(mid), mid, mtype))
        if cur.rowcount:
            rowid = cur.lastrowid
            c.execute('INSERT INTO fts_trigram(rowid, content) VALUES(?,?)', (rowid, clean))
            c.execute('INSERT INTO fts_jieba(rowid, tokens) VALUES(?,?)', (rowid, toks))
            inserted += 1
            # 只对 user/assistant 记录做增量向量化(tool 是噪音, 跳过)
            if role in ('user', 'assistant', 'human', 'ai'):
                new_ids.append(rowid)
                new_texts.append(strip_system_prefix(clean)[:600] or clean[:600])
    c.commit()

    # 增量向量化(批量16条)
    if new_ids:
        try:
            for i in range(0, len(new_ids), 16):
                chunk = new_ids[i:i + 16]
                texts = new_texts[i:i + 16]
                V = encode_texts(texts)
                if V is None:
                    break
                c.executemany('UPDATE ledger SET embed=?, embed_model=? WHERE id=?',
                              [(V[j].tobytes(), EMBED_MODEL, rid)
                               for j, rid in enumerate(chunk)])
                vec_added += len(chunk)
            c.commit()
        except Exception as e:
            sys.stderr.write('embed failed: %s\n' % e)

    c.execute("INSERT OR REPLACE INTO meta VALUES('last_run',?)", (str(now),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_inserted',?)", (str(inserted),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_embedded',?)", (str(vec_added),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_msg_id',?)", (str(st['last_msg_id']),))
    c.commit(); c.close(); h.close()
    save_state(st)
    print('inserted=%d skipped_high=%d embedded=%d last_id=%d'
          % (inserted, skipped_high, vec_added, st['last_msg_id']))


if __name__ == '__main__':
    main()
