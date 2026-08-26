#!/usr/bin/env bash
# dev-stop.sh — stop the local dev app started by dev-start.sh
set -euo pipefail; cd "$(dirname "$0")/.."
pid_file=".local/server.pid"
if [[ ! -f "$pid_file" ]]; then
  echo "no dev server running (missing $pid_file)" >&2
  exit 1
fi
pid="$(cat "$pid_file")"
if ! kill "$pid" 2>/dev/null; then
  echo "dev server PID $pid not running" >&2
  rm -f "$pid_file"
  exit 1
fi
rm -f "$pid_file"
echo "stopped dev server (pid $pid)"
