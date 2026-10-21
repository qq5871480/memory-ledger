#!/bin/bash
# memory-ledger 一键安装（Hermes Agent 适用，其他 Agent 改 HERMES_DB 后同样可用）
set -e
LEDGER_DIR="${LEDGER_DIR:-/opt/memory-ledger}"
echo "==> 安装到 $LEDGER_DIR"
[ -d "$LEDGER_DIR" ] || { sudo mkdir -p "$LEDGER_DIR"; sudo chown "$(id -un):$(id -gn)" "$LEDGER_DIR"; }

echo "==> 检查依赖"
PY="${PYBIN:-python3}"
$PY -c "import jieba" 2>/dev/null || pip3 install jieba

echo "==> 部署文件"
cp collector.py query.py snapshot.sh selfcheck.sh restore.sh push_alert.py last_run_age_helper.py user_dict.txt "$LEDGER_DIR/"
chmod +x "$LEDGER_DIR"/*.sh

echo "==> 初始化库（空库，等待首次采集）"
cd "$LEDGER_DIR" && $PY -c "
import sys; sys.path.insert(0, '.')
from collector import init_db
init_db()
print('库初始化完成'
"

echo "==> 建议的 cron（*/5 采集 / 每6h快照 / 每小时自检）"
cat <<CRON
*/5 * * * * cd $LEDGER_DIR && $PY collector.py >> collector.log 2>&1
0 */6 * * * $LEDGER_DIR/snapshot.sh
35 * * * * $LEDGER_DIR/selfcheck.sh
CRON
echo "==> 完成。采集源默认读 ~/.hermes/state.db，其他 Agent 改 HERMES_DB 环境变量或 collector.py 里的 SQL。"
echo "==> 验证: $PY $LEDGER_DIR/query.py '测试'"
