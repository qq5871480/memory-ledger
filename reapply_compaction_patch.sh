#!/bin/bash
# memory-ledger 压缩恢复指针补丁重打脚本
# 用途：Hermes升级（rsync覆盖官方源码）后，把"小账本查询指针"重新注入压缩恢复提示
# 用法：bash /opt/memory-ledger/reapply_compaction_patch.sh
set -e
CC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}/agent/context_compressor.py"
VENVPY="${HERMES_VENV_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"

if grep -q "BONUS LEDGER" "$CC"; then
  echo "[skip] patch already present"
  exit 0
fi

"$VENVPY" /opt/memory-ledger/apply_patch.py "$CC"
"$VENVPY" -m py_compile "$CC" && echo "[ok] compile pass"
echo "[done] patched. restart gateway via standard channel to take effect"
