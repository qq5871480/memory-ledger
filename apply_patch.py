"""把小账本查询指针注入 Hermes 压缩恢复提示（升级后重打补丁用）。

用法: python3 apply_patch.py /path/to/context_compressor.py
幂等: 已打补丁时跳过。锚文本来自官方 v2026.9.24 原文，上游若改动会显式报错。
"""
import sys

path = sys.argv[1]
src = open(path).read()

if "BONUS LEDGER" in src:
    print("[skip] patch already present")
    sys.exit(0)

old = (
    '        f"session_search(query=\'<keywords>\', session_id=\'{session_id}\') — "\n'
    '        "do not guess at lost specifics when you can look them up."\n'
    '    )'
)
bsn = chr(92) + "n"  # literal backslash-n as it must appear in the generated source
new = (
    '        f"session_search(query=\'<keywords>\', session_id=\'{session_id}\') — "\n'
    '        "do not guess at lost specifics when you can look them up.' + bsn + '"\n'
    '        "BONUS LEDGER: for details older than this session or cross-session facts, also query "\n'
    '        "the external memory ledger: "\n'
    '        "`python3 /opt/memory-ledger/query.py \'<keywords>\'` (Chinese FTS, all past sessions, "\n'
    '        "results tagged [历史存档]; try 2-3 keyword variants if first query misses)."\n'
    '    )'
)

if old not in src:
    sys.exit("[FAIL] anchor not found - context_compressor.py changed upstream, re-inspect manually")

open(path, "w").write(src.replace(old, new, 1))
print("[ok] patched")
