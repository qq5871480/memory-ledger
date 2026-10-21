#!/bin/bash
# 记忆账本自检（可用环境变量覆盖路径）
LEDGER_DIR="${LEDGER_DIR:-"$LEDGER_DIR"}"
PYBIN="${PYBIN:-python3}": 3次失败(约30min)告警; 24h断流升级; 每小时最多提醒一次
set -u
LOG="$LEDGER_DIR"/collector.log
LAST_RUN=$("$PYBIN" "$LEDGER_DIR"/last_run_age_helper.py 2>/dev/null)
NOW=$(date +%s)
AGE_MIN=$(( (NOW - ${LAST_RUN:-0}) / 60 ))
ALERT=""
if [ "$AGE_MIN" -ge 1440 ]; then
  ALERT="记忆账本已${AGE_MIN}分钟未入库(超24h,严重)"
elif [ "$AGE_MIN" -ge 40 ]; then
  ALERT="记忆账本${AGE_MIN}分钟未入库(疑似断流)"
fi
if [ -n "$ALERT" ]; then
  STAMP=$(date +%Y%m%d%H)
  LAST=$(cat "$LEDGER_DIR"/.alert_stamp 2>/dev/null || echo 0)
  if [ "$STAMP" != "$LAST" ]; then
    echo "$STAMP" > "$LEDGER_DIR"/.alert_stamp
    echo "$(date '+%m-%d %H:%M') ALERT $ALERT" >> "$LOG"
    ALERT="$ALERT" "$PYBIN" "$LEDGER_DIR"/push_alert.py
  fi
fi
echo "$(date '+%m-%d %H:%M') selfcheck age=${AGE_MIN}min" >> "$LOG"
