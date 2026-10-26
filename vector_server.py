#!/usr/bin/env python3
"""向量服务 v1.0 — 常驻模型, HTTP接口提供编码/检索

问题: query.py 每次调用都重新加载 bge 模型(92MB), 冷启动 8.4 秒
      关键词路只需 0.83 秒 —— 10倍差距来自模型加载
方案: 常驻小服务, 模型加载一次, 查询走 HTTP (本地回环, 约5ms)

接口:
  POST /encode  {"texts": ["...", ...]}       -> {"vectors": [[...], ...]}  (已归一化)
  POST /search  {"query": "...", "top": 20}    -> {"results": [{"id":..,"sim":..}]}
  GET  /health                                -> {"ok": true, "model": "...", "count": N}
"""
import json
import os
import sqlite3
import threading
import time

import numpy as np

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'
MODEL_TAG = 'bge-small-zh-v1.5'
DIM = 512
PREFIX = '为这个句子生成表示以用于检索相关文章：'
HOST = '127.0.0.1'
PORT = int(os.environ.get('VEC_PORT', '9137'))

_state = {'tok': None, 'mdl': None, 'loaded': False}
_lock = threading.Lock()


def get_model():
    if _state['loaded']:
        return _state['tok'], _state['mdl']
    with _lock:
        if _state['loaded']:
            return _state['tok'], _state['mdl']
        os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
        import torch
        from transformers import AutoTokenizer, AutoModel
        torch.set_num_threads(max(1, (os.cpu_count() or 4)))
        tok = AutoTokenizer.from_pretrained('BAAI/' + MODEL_TAG)
        mdl = AutoModel.from_pretrained('BAAI/' + MODEL_TAG)
        mdl.eval()
        _state['tok'] = tok
        _state['mdl'] = mdl
        _state['loaded'] = True
        return tok, mdl


def encode(texts, prefix=''):
    tok, mdl = get_model()
    if prefix:
        texts = [prefix + t for t in texts]
    b = tok(texts, padding=True, truncation=True, max_length=512, return_tensors='pt')
    import torch
    with torch.no_grad():
        o = mdl(**b).last_hidden_state[:, 0]
    v = o.numpy().astype('float32')
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def load_matrix():
    c = sqlite3.connect('file:%s?mode=ro' % LEDGER, uri=True)
    rows = c.execute(
        "SELECT id, embed FROM ledger WHERE embed IS NOT NULL AND status='active'").fetchall()
    c.close()
    ids, vecs = [], []
    for rid, blob in rows:
        if blob is None or len(blob) != DIM * 2:
            continue
        ids.append(rid)
        vecs.append(np.frombuffer(blob, dtype=np.float16).astype('float32'))
    if not ids:
        return np.zeros((0, DIM), dtype='float32'), []
    M = np.vstack(vecs)
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    return M, ids


_matrix = {'M': None, 'ids': [], 'ts': 0.0}
_cache_ttl = 30.0  # 秒


def get_matrix():
    with _lock:
        if _matrix['M'] is None or (time.time() - _matrix['ts']) > _cache_ttl:
            M, ids = load_matrix()
            _matrix['M'] = M
            _matrix['ids'] = ids
            _matrix['ts'] = time.time()
        return _matrix['M'], _matrix['ids']


def handle(path, body):
    if path == '/health':
        M, ids = get_matrix()
        return {'ok': True, 'model': MODEL_TAG, 'count': len(ids),
                'loaded': _state['loaded']}
    if path == '/encode':
        texts = body.get('texts') or []
        if not texts:
            return {'vectors': []}
        v = encode(texts, prefix=body.get('prefix', ''))
        return {'vectors': v.astype('float16').tolist()}
    if path == '/search':
        q = body.get('query') or ''
        top = int(body.get('top') or 20)
        if not q:
            return {'results': []}
        qv = encode([q], prefix=PREFIX)[0]
        M, ids = get_matrix()
        if M.size == 0:
            return {'results': []}
        sims = M @ qv
        order = np.argsort(-sims)[:top]
        return {'results': [{'id': int(ids[i]), 'sim': float(sims[i])} for i in order]}
    return {'error': 'not found'}


def main():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                self._send(handle(self.path.split('?')[0], {}))
            except Exception as e:
                self._send({'error': str(e)}, 500)

        def do_POST(self):
            try:
                n = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                self._send(handle(self.path.split('?')[0], body))
            except Exception as e:
                self._send({'error': str(e)}, 500)

    # 预热模型(避免第一个请求等8秒)
    print('preloading model...', flush=True)
    t = time.time()
    get_model()
    print('model loaded in %.1fs' % (time.time() - t), flush=True)
    M, ids = get_matrix()
    print('matrix: %d x %d' % (M.shape if len(M.shape) == 2 else (0, 0)), flush=True)

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print('serving on %s:%d' % (HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
