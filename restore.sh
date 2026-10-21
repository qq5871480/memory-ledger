#!/bin/bash
# 记忆账本恢复runbook v1.3（目标单人30分钟内完成RTO）
# 用法: restore.sh [日期YYYY-MM-DD]   默认取最近一份
set -eu
LEDGER="${LEDGER_DIR:-/opt/memory-ledger}/ledger.db"
LEDGER_DIR="${LEDGER_DIR:-/opt/memory-ledger}"
LOCAL_BAK=/var/backups/hermes
NAS_DIR=/NAS/backup/hermes-memory
DATE=${1:-}
STEP() { echo "[$(date '+%H:%M:%S')] $1"; }

STEP "1/8 定位快照"
if [ -z "$DATE" ]; then
  SNAP=$(ls -t "$LOCAL_BAK"/ledger-*.db 2>/dev/null | head -1 || true)
  [ -z "$SNAP" ] && SNAP=$(ls -t "$NAS_DIR"/*/ledger-*.db 2>/dev/null | head -1 || true)
else
  SNAP="$LOCAL_BAK/ledger-$DATE.db"
  [ -f "$SNAP" ] || SNAP="$NAS_DIR/$DATE/ledger-$DATE.db"
fi
[ -f "$SNAP" ] || { echo "找不到快照"; exit 1; }
STEP "找到快照: $SNAP"

STEP "2/8 sha256校验"
cd "$(dirname "$SNAP")"
sha256sum -c "$(basename "$SNAP").sha256"

STEP "3/8 完整性检查"
PRAGMA_OK=$(python3 -c "
import sqlite3
c = sqlite3.connect('file:$SNAP?mode=ro', uri=True)
r = c.execute('PRAGMA integrity_check').fetchone()[0]
print(r)")
[ "$PRAGMA_OK" = "ok" ] || { echo "完整性检查失败: $PRAGMA_OK"; exit 1; }
STEP "integrity_check ok"

STEP "4/8 停采集cron"
systemctl --user stop memory-ledger-collect.timer 2>/dev/null || crontab_wrap=1
STEP "cron已停"

STEP "5/8 还原数据库"
cp "$SNAP" "$LEDGER.restore"
chown "$(id -un):$(id -gn)" "$LEDGER.restore"
chmod 600 "$LEDGER.restore"
mv "$LEDGER.restore" "$LEDGER"
rm -f "$LEDGER-wal" "$LEDGER-shm" "$LEDGER-journal"

STEP "6/8 完整性复查"
python3 -c "
import sqlite3
c = sqlite3.connect('$LEDGER')
r = c.execute('PRAGMA integrity_check').fetchone()[0]
n = c.execute('SELECT COUNT(*) FROM ledger').fetchone()[0]
print('integrity:', r, '| rows:', n)
[ r = 'ok' ]"

STEP "7/8 恢复cron"
systemctl --user start memory-ledger-collect.timer 2>/dev/null || true

STEP "8/8 抽样验证"
python3 "$LEDGER_DIR"/query.py 项目 | head -5

STEP "恢复完成"
