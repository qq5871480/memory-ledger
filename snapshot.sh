#!/bin/bash
# 记忆账本快照（环境变量可覆盖）
LEDGER_DIR="${LEDGER_DIR:-/opt/memory-ledger}"
LOCAL_BAK="${BACKUP_DIR:-/var/backups/hermes}"
NAS_DIR="${NAS_DIR:-/NAS/backup/hermes-memory}"+NAS同步 v1.3（.backup一致性快照，禁直拷活库）
set -u
LEDGER="$LEDGER_DIR/ledger.db"
# LOCAL_BAK above
# NAS_DIR above
DATE=$(date +%Y-%m-%d)
LOG="$LEDGER_DIR/snapshot.log"
[ -d "$LOCAL_BAK" ] || { sudo mkdir -p "$LOCAL_BAK"; sudo chown "$(id -un):$(id -gn)" "$LOCAL_BAK"; }
SNAP="$LOCAL_BAK/ledger-$DATE.db"
echo "$(date '+%m-%d %H:%M:%S') snapshot start" >> "$LOG"
sqlite3 "$LEDGER" ".backup '$SNAP'" 2>>"$LOG" || { echo "$(date) BACKUP FAIL" >> "$LOG"; exit 1; }
chmod 600 "$SNAP"
sha256sum "$SNAP" > "$SNAP.sha256"
if [ -d "$NAS_DIR" ]; then
  ok=0
  for i in 1 2 3; do
    rsync -a --chmod=600 "$SNAP" "$SNAP.sha256" "$NAS_DIR/$DATE/" 2>>"$LOG" && { ok=1; break; }
    sleep 30
  done
  [ $ok -eq 1 ] && echo "$(date '+%m-%d %H:%M:%S') NAS sync ok" >> "$LOG" || echo "$(date '+%m-%d %H:%M:%S') NAS sync FAIL(3)" >> "$LOG"
else
  echo "$(date '+%m-%d %H:%M:%S') NAS not mounted, local only" >> "$LOG"
fi
# 快照保留：7份滚动+每月1日档永久留（1022收紧：13份478MB→约7份250MB）
find "$LOCAL_BAK" -name 'ledger-*.db' -mtime +7 ! -name 'ledger-*-01.db' -delete 2>/dev/null
find "$LOCAL_BAK" -name 'ledger-*-01.db' -mtime +400 -delete 2>/dev/null
echo "$(date '+%m-%d %H:%M:%S') done" >> "$LOG"
