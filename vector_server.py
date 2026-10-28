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
_matrix_lock = threading.Lock()   # 只保护矩阵重载, 不阻塞推理


def get_matrix():
    """取向量矩阵。命中缓存直接返回; 过期则重载。

    1028压力测试发现: 高并发下 ConnectionResetError。
    两个根因:
      1) ThreadingHTTPServer 默认 listen backlog = 5, 30+并发时accept队列溢出
      2) BaseHTTPRequestHandler 默认 HTTP/1.0, 每请求新建TCP连接, 高并发时端口耗尽
    修法: request_queue_size=128 + protocol_version='HTTP/1.1' keep-alive。
    另外矩阵重载(读1500+行sqlite)会阻塞所有请求, 改为后台线程预热。
    """
    with _matrix_lock:
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
    import socketserver

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 keep-alive: 高并发下复用TCP连接, 避免端口耗尽(1028压测)
        protocol_version = 'HTTP/1.1'
        # 单请求体上限, 防超大payload打爆内存
        max_body = 8 * 1024 * 1024

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
                try:
                    self._send({'error': str(e)}, 500)
                except Exception:
                    pass

        def do_POST(self):
            try:
                n = int(self.headers.get('Content-Length', 0))
                if n > self.max_body:
                    self._send({'error': 'payload too large'}, 413)
                    return
                body = json.loads(self.rfile.read(n)) if n else {}
                self._send(handle(self.path.split('?')[0], body))
            except Exception as e:
                try:
                    self._send({'error': str(e)}, 500)
                except Exception:
                    pass

    class RobustServer(ThreadingHTTPServer):
        # listen backlog: 默认5太小, 高并发时accept队列溢出 → ConnectionReset
        request_queue_size = 128
        daemon_threads = True
        allow_reuse_address = True

    # 预热模型(避免第一个请求等8秒)
    print('preloading model...', flush=True)
    t = time.time()
    get_model()
    print('model loaded in %.1fs' % (time.time() - t), flush=True)
    M, ids = get_matrix()
    print('matrix: %d x %d' % (M.shape if len(M.shape) == 2 else (0, 0)), flush=True)

    # 后台线程定期预热矩阵(不阻塞请求处理)
    def _refresher():
        while True:
            time.sleep(20)
            try:
                get_matrix()
            except Exception:
                pass

    threading.Thread(target=_refresher, daemon=True).start()

    srv = RobustServer((HOST, PORT), Handler)
    print('serving on %s:%d (backlog=%d)' % (HOST, PORT, RobustServer.request_queue_size), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
