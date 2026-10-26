#!/usr/bin/env python3
"""记忆账本向量回填 v2.0 — 本地 bge-small-zh-v1.5

设计要点:
1. 剥掉 [IMPORTANT: ...] 系统注入前缀(cron注入),避免模板噪音霸占TopN
2. 按剥离后文本去重,避免同内容重复编码
3. float16 存储 BLOB,体积减半
4. 断点续传:已有 embed 的记录跳过
5. 只编码 user/assistant 角色,tool 记录跳过(占81%且是噪音)
"""
import os
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
import re
import sqlite3
import sys
import time

import numpy as np
import torch

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
MODEL_NAME = 'BAAI/bge-small-zh-v1.5'
MODEL_TAG = 'bge-small-zh-v1.5'
DIM = 512
BATCH = 16
MAX_CHARS = 600
PREFIX = '为这个句子生成表示以用于检索相关文章：'

# 剥离系统注入前缀: [IMPORTANT: ...] 开头直到第一个换行后的实际内容
_SYS_PATTERNS = [
    re.compile(r'^\[IMPORTANT[^\]]*\]\s*', re.S),
    re.compile(r'^\[System[^\]]*\]\s*', re.S),
    re.compile(r'^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*', re.S),
]


def clean_text(text):
    """剥离系统注入前缀,返回(清洗后文本, 是否被剥离)"""
    if not text:
        return '', False
    orig = text
    changed = False
    for pat in _SYS_PATTERNS:
        new = pat.sub('', text)
        if new != text:
            text = new
            changed = True
    # 剥离残留的 IMPORTANT 标记行(可能在正文中间)
    text = re.sub(r'\[IMPORTANT[^\]]*\]', '', text)
    text = text.strip()
    return text, (changed or text != orig.strip())


def load_model():
    from transformers import AutoTokenizer, AutoModel
    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    mdl = AutoModel.from_pretrained(MODEL_NAME)
    mdl.eval()
    torch.set_num_threads(max(1, (os.cpu_count() or 4)))
    return tok, mdl


def encode(tok, mdl, texts, prefix=''):
    if prefix:
        texts = [prefix + t for t in texts]
    b = tok(texts, padding=True, truncation=True, max_length=512, return_tensors='pt')
    with torch.no_grad():
        o = mdl(**b).last_hidden_state[:, 0]
    v = o.numpy().astype('float32')
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v.astype('float16')


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    only_missing = '--all' not in sys.argv

    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=10000')

    if only_missing:
        sql = ("SELECT id, content FROM ledger "
               "WHERE role IN ('user','assistant') AND embed IS NULL "
               "ORDER BY id")
    else:
        sql = ("SELECT id, content FROM ledger "
               "WHERE role IN ('user','assistant') ORDER BY id")
    rows = c.execute(sql).fetchall()

    # 清洗 + 去重
    seen = {}
    cleaned = []
    n_strip = 0
    for rid, content in rows:
        txt, stripped = clean_text(content)
        if stripped:
            n_strip += 1
        if not txt or len(txt) < 8:
            continue
        key = txt[:200]
        if key in seen:
            continue
        seen[key] = rid
        cleaned.append((rid, txt))

    print('待处理 %d 条 -> 清洗去重后 %d 条 (剥前缀 %d 条)'
          % (len(rows), len(cleaned), n_strip), flush=True)
    if limit:
        cleaned = cleaned[:limit]
    if not cleaned:
        print('无待处理记录')
        return

    tok, mdl = load_model()
    print('模型已加载', flush=True)

    t0 = time.time()
    done = 0
    for i in range(0, len(cleaned), BATCH):
        chunk = cleaned[i:i + BATCH]
        try:
            V = encode(tok, mdl, [t[:MAX_CHARS] for _, t in chunk])
        except Exception as e:
            print('批次 %d 失败: %s' % (i, e), flush=True)
            continue
        rows_up = [(rid, V[j].tobytes(), MODEL_TAG) for j, (rid, _) in enumerate(chunk)]
        c.executemany('UPDATE ledger SET embed=?, embed_model=? WHERE id=?',
                      [(b, m, rid) for rid, b, m in rows_up])
        c.commit()
        done += len(chunk)
        el = time.time() - t0
        rate = done / max(el, 0.001)
        left = (len(cleaned) - done) / max(rate, 0.001)
        print('  %d/%d  %.0f条/秒  剩余%.0f秒' % (done, len(cleaned), rate, left), flush=True)

    n = c.execute("SELECT COUNT(*) FROM ledger WHERE embed IS NOT NULL").fetchone()[0]
    sz = c.execute("SELECT SUM(LENGTH(embed)) FROM ledger WHERE embed IS NOT NULL").fetchone()[0]
    print('完成: 已向量化 %d 条, 向量总大小 %.1fMB, 总耗时 %.0f秒'
          % (n, (sz or 0) / 1024 / 1024, time.time() - t0), flush=True)
    c.close()


if __name__ == '__main__':
    main()
