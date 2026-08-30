#!/usr/bin/env bash
# BrainForge — NotebookLM session keepalive
# Keeps the persisted session fresh by rotating __Secure-1PSIDTS every 15 min
# (the cadence officially recommended by notebooklm-py for the keepalive path).
# Run in background:  nohup ./keepalive.sh >/tmp/nblm-keepalive.log 2>&1 &
# Or: ./keepalive.sh check   (one-shot refresh + status)

set -u
INTERVAL="${KEEPALIVE_INTERVAL:-900}"   # 15 minutes
LOG="/tmp/nblm-keepalive.log"
mkdir -p "$(dirname "$LOG")"

refresh() {
  local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
  if notebooklm auth refresh >>"$LOG" 2>&1; then
    echo "[$ts] refresh OK" >>"$LOG"
  else
    echo "[$ts] refresh FAILED (session may be invalid)" >>"$LOG"
  fi
}

status() {
  if notebooklm auth check --test 2>&1 | grep -q "Authentication is valid"; then
    echo "VALID"
  else
    echo "INVALID"
  fi
}

if [ "${1:-loop}" = "check" ]; then
  refresh
  echo "status: $(status)"
  exit 0
fi

echo "[keepalive] started, interval=${INTERVAL}s (log: $LOG)"
while true; do
  refresh
  sleep "$INTERVAL"
done
