#!/bin/sh
# entrypoint.sh — prepare runtime then exec the container command.
# Ensures the /data volume (bind-mounted on Unraid) exists so the SQLite
# database at FM_DATABASE_URL=/data/filaments.db can be created on first run.
set -e
mkdir -p /data
exec "$@"
