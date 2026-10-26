"""把小账本查询指针注入 Hermes 压缩恢复提示（升级后重打补丁用）。

用法: python3 apply_patch.py /path/to/context_compressor.py
幂等: 已打补丁时跳过。锚文本来自官方 v2026.9.24 原文，上游若改动会显式报错。

v2.0 (1027): footer 升级为双路召回说明 + 强制查询条款（先查再答，不许凭印象编）。
旧版footer只写"Chinese FTS"，升级后重打会把新版覆盖成旧版 —— 故此处检测到旧版时
显式提示需要人工确认，不静默跳过。
"""
import sys

path = sys.argv[1]
src = open(path).read()

MARKER = "BONUS LEDGER"
MODERN = "local vector recall"

if MODERN in src:
    print("[skip] patch already present (v2.0 modern footer)")
    sys.exit(0)

if MARKER in src:
    print("[WARN] 发现旧版 BONUS LEDGER footer (无 '" + MODERN + "' 字样)。")
    print("       升级后重打本补丁会把 footer 覆盖成旧版单链文案。")
    print("       请人工确认 context_compressor.py 的 footer 是否需要升级为双路召回版本。")
    sys.exit(2)

bsn = chr(92) + "n"  # literal backslash-n as it must appear in the generated source

# 官方原文锚点（v2026.9.24）:
#   "do not guess at lost specifics when you can look them up.\n"
#   "    )"
# 注意 up 后面是 . + 反斜杠n + 引号, 不是 . + 引号
old_candidates = [
    '        f"session_search(query=\'<keywords>\', session_id=\'{session_id}\') — "\n'
    '        "do not guess at lost specifics when you can look them up.' + bsn + '"\n'
    '    )',
    '        f"session_search(query=\'<keywords>\', session_id=\'{session_id}\') — "\n'
    '        "do not guess at lost specifics when you can look them up."\n'
    '    )',
]

new = (
    '        f"session_search(query=\'<keywords>\', session_id=\'{session_id}\') — "\n'
    '        "do not guess at lost specifics when you can look them up.' + bsn + '"\n'
    '        "BONUS LEDGER: for details older than this session or cross-session facts, also query "\n'
    '        "the external memory ledger: "\n'
    '        "`python3 /opt/memory-ledger/query.py \'<keywords>\'` — it now does BOTH keyword "\n'
    '        "(jieba/trigram/LIKE) AND local vector recall (bge-small-zh-v1.5, 512d), so vague "\n'
    '        "phrases work too. Results tagged [历史存档]; try 2-3 keyword variants if first query misses. "\n'
    '        "IMPORTANT: when the answer depends on any detail not visible in the current context "\n'
    '        "(past decisions, old numbers, prior conversations, \'what did we do about X last time\'), "\n'
    '        "you MUST run this query BEFORE answering — never answer from impression."\n'
    '    )'
)

anchor = None
for cand in old_candidates:
    if cand in src:
        anchor = cand
        break

if anchor is None:
    sys.exit("[FAIL] anchor not found - context_compressor.py changed upstream, re-inspect manually")

open(path, "w").write(src.replace(anchor, new, 1))
print("[ok] patched (v2.0 modern footer)")
