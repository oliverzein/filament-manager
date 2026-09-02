#!/usr/bin/env bash
# dev-start.sh — start app locally with isolated state
set -euo pipefail; cd "$(dirname "$0")/.."
mkdir -p .local
export FM_DATABASE_URL="sqlite:///.local/filaments.db"
export FM_PRINTER_IP="${FM_PRINTER_IP:-192.168.0.101}"
export FM_SSH_USER="${FM_SSH_USER:-root}" FM_SSH_PASSWORD="${FM_SSH_PASSWORD:-}"
export FM_DB_REMOTE_PATH="/mnt/UDISK/creality/userdata/box/material_database.json"
nohup python -m uvicorn fm.api.app:create_app --factory --host 0.0.0.0 --port 8404 >.local/server.log 2>&1 &
echo $! > .local/server.pid
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8404/api/version >/dev/null && exit 0; sleep 1; done
echo "server did not become ready"; tail -50 .local/server.log; exit 1
