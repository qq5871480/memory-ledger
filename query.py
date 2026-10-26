#!/usr/bin/env python3
"""记忆账本检索 v2.0 — 三链FTS(jieba/trigram/LIKE) + 本地向量双路召回

向量路:
  - 本地 bge-small-zh-v1.5 (512维, float16 存储)
  - numpy 暴力检索(7293条级别 2ms 内, 不需要向量数据库)
  - 关键词命中给 boost, 保证精确查询不被语义稀释
"""
import sqlite3
import sys
import time
import argparse
import re
import os

import jieba

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
JIEBA_DICT = LEDGER_DIR + '/user_dict.txt'
TOP_K = 5
BUDGET = 2000
HALF_LIFE = {'fact': 90 * 86400, 'preference': 90 * 86400, 'state': 7 * 86400,
             'procedure': 3650 * 86400, 'experience': 180 * 86400}

# 向量模型
MODEL_TAG = 'bge-small-zh-v1.5'
EMBED_DIM = 512
PREFIX = '为这个句子生成表示以用于检索相关文章：'

_model_cache = {}


def get_model():
    """懒加载模型(约400MB内存, 平时不用不占)"""
    if 'tok' in _model_cache:
        return _model_cache['tok'], _model_cache['mdl']
    os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
    try:
        import torch
        from transformers import AutoTokenizer, AutoModel
        torch.set_num_threads(max(1, (os.cpu_count() or 4)))
        tok = AutoTokenizer.from_pretrained('BAAI/bge-small-zh-v1.5')
        mdl = AutoModel.from_pretrained('BAAI/bge-small-zh-v1.5')
        mdl.eval()
        _model_cache['tok'] = tok
        _model_cache['mdl'] = mdl
        return tok, mdl
    except Exception as e:
        sys.stderr.write('[vec] 模型加载失败, 降级为纯关键词: %s\n' % e)
        return None, None


def encode_query(text):
    """把查询编码成向量"""
    tok, mdl = get_model()
    if tok is None:
        return None
    import torch
    b = tok([PREFIX + text], padding=True, truncation=True, max_length=512, return_tensors='pt')
    with torch.no_grad():
        o = mdl(**b).last_hidden_state[:, 0]
    v = o.numpy().astype('float32')[0]
    import numpy as np
    n = np.linalg.norm(v)
    if n == 0:
        return None
    return v / n


def vector_search(c, qvec, top_k=TOP_K * 4):
    """numpy 暴力检索"""
    import numpy as np
    rows = c.execute(
        "SELECT id, embed FROM ledger WHERE embed IS NOT NULL AND status='active'").fetchall()
    if not rows:
        return []
    ids = []
    vecs = []
    for rid, blob in rows:
        if blob is None or len(blob) != EMBED_DIM * 2:
            continue
        ids.append(rid)
        vecs.append(np.frombuffer(blob, dtype=np.float16).astype('float32'))
    if not ids:
        return []
    M = np.vstack(vecs)
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    sims = M @ qvec
    order = np.argsort(-sims)[:top_k]
    return [(int(ids[i]), float(sims[i])) for i in order]


if os.path.exists(JIEBA_DICT):
    jieba.load_userdict(JIEBA_DICT)


def connect():
    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=5000')
    return c


def tokenize(text):
    return ' '.join(w for w in jieba.cut_for_search(text) if w.strip())


def search(keyword, top_k=TOP_K, days=None, session=None, role=None, semantic=True,
           exclude_session=None):
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

    # 向量路: 模糊提问召回
    vec_scores = {}
    if semantic:
        qvec = encode_query(keyword)
        if qvec is not None:
            for rid, sim in vector_search(c, qvec, top_k * 4):
                if rid not in results:
                    results.append(rid)
                vec_scores[rid] = sim

    out = []
    q_norm = keyword.strip()
    echo_penalty = 0.35  # 自己复述/引用查询词的回答会被降权
    # 候选集: 关键词路 + 向量路, 向量路必须完整保留(不能被关键词结果挤掉)
    kw_candidates = list(results)
    vec_candidates = [rid for rid in vec_scores if rid not in kw_candidates]
    candidates = kw_candidates[: top_k * 4] + vec_candidates
    for rid in candidates:
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
        if exclude_session and sess == exclude_session:
            continue
        hl = HALF_LIFE.get(typ, 30 * 86400)
        decay = 0.5 ** ((now - ts) / hl)
        trust_b = {'high': 1.0, 'medium': 0.7, 'low': 0.4}.get(tl, 0.5)
        score = decay * trust_b
        # tool 记录是执行噪音(占81%), 没有向量, 只靠关键词进来 -> 明显降权
        if row_role == 'tool':
            score *= 0.25
        m = re.search(re.escape(keyword), content)
        kw_hit = bool(m)
        if kw_hit:
            score *= 1.6  # 关键词精确命中给 boost
        if rid in vec_scores:
            # 语义相似度加成(0~1), 映射到 0.3~0.9 权重
            score *= (0.3 + 0.6 * max(0.0, vec_scores[rid]))
        # 回声降权: assistant 消息里逐字复述了整条查询 -> 大概率是我自己刚写的
        # 分析/复述稿, 不是原始事实记录
        if row_role == 'assistant' and len(q_norm) >= 8 and q_norm in content:
            score *= echo_penalty
        snip = ''
        if m:
            st = max(0, m.start() - 100)
            snip = content[st:st + 400]
        else:
            snip = content[:400]
        out.append((score, rid, ts, sess, row_role, snip, stype, tl, origin, kw_hit,
                    vec_scores.get(rid, 0.0)))
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
    for score, rid, ts, sess, row_role, snip, stype, tl, origin, kw_hit, sim in final:
        tstr = time.strftime('%m-%d %H:%M', time.localtime(ts))
        tags = []
        tags.append('词' if kw_hit else '义')
        if sim:
            tags.append('sim%.2f' % sim)
        block = '[%s|%s|%s/%s|%s] ...%s... (id:%s src:%s)' % (
            tstr, row_role, stype, tl, '/'.join(tags), snip, rid, origin)
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
    ap.add_argument('--no-semantic', action='store_true', help='禁用向量路, 只用关键词')
    ap.add_argument('--exclude-session', default=None, help='排除指定session(避免本次会话内容污染检索)')
    a = ap.parse_args()
    roles = [r.strip() for r in a.role.split(',')] if a.role else None
    final = search(a.keyword, a.top, a.days, a.session, role=roles,
                   semantic=not a.no_semantic, exclude_session=a.exclude_session)
    print(format_out(final, a.keyword))
