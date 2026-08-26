#!/usr/bin/env bash
# deploy-unraid.sh <version> — load the released image onto the Unraid host
# and restart the filament-manager container.
#
#   scripts/deploy-unraid.sh 0.1.0
#
# Prerequisites:
#   - dist/filament-manager-<version>.tar.gz produced by scripts/release.sh
#   - an env file with the FM_* variables at deploy/filament-manager.env
#     (or pass the path via FM_ENV_FILE). Never commit real credentials.
#   - sshpass + ssh access to root@unraid (or an ssh config alias "unraid").
#
# The Unraid host IP defaults to 192.168.0.31 (override with UNRAID_IP).
# This script is ready to run against the real host but is NOT executed by
# the build/test pipeline — run it manually with real credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <version>   (e.g. 0.1.0)" >&2
  exit 2
fi
version="$1"
image="filament-manager:${version}"
tarball="dist/filament-manager-${version}.tar.gz"
host="${UNRAID_HOST:-unraid}"
ip="${UNRAID_IP:-192.168.0.31}"
remote_dir="/mnt/user/appdata/filament-manager"
env_file="${FM_ENV_FILE:-deploy/filament-manager.env}"
template="deploy/filament-manager.xml"
templates_dir="/boot/config/plugins/dockerMan/templates-user"
container="filament-manager"

if [[ ! -f "${tarball}" ]]; then
  echo "error: ${tarball} not found; run scripts/release.sh ${version} first" >&2
  exit 1
fi
if [[ ! -f "${env_file}" ]]; then
  echo "error: env file ${env_file} not found" >&2
  exit 1
fi
if [[ ! -f "${template}" ]]; then
  echo "error: template ${template} not found" >&2
  exit 1
fi

echo "==> copying ${tarball} to ${host}:${remote_dir}/"
sshpass -e scp -O "${tarball}" "root@${host}:${remote_dir}/"
# Provide the ssh password via SSHPASS (sshpass -e) if set; otherwise rely on
# ssh keys / an ssh config alias. Export only when defined.
export SSHPASS="${SSHPASS:-}"

echo "==> loading image on ${host}"
sshpass -e ssh "root@${host}" "gunzip -c ${remote_dir}/$(basename "${tarball}") | docker load"

echo "==> installing dockerMan template"
sshpass -e scp -O "${template}" "root@${host}:${templates_dir}/$(basename "${template}")"

echo "==> restarting container ${container}"
sshpass -e ssh "root@${host}" "
  docker stop ${container} 2>/dev/null || true
  docker rm ${container} 2>/dev/null || true
  docker run -d \
    --name ${container} \
    --env-file ${remote_dir}/filament-manager.env \
    -v ${remote_dir}:/data \
    -p 8404:8000 \
    --restart unless-stopped \
    ${image}
"

echo "==> polling http://${ip}:8404/api/version"
for i in $(seq 1 30); do
  if curl -sf "http://${ip}:8404/api/version" >/dev/null; then
    echo "filament-manager ${version} is up at http://${ip}:8404"
    curl -sf "http://${ip}:8404/api/version"
    echo
    exit 0
  fi
  sleep 1
done
echo "error: container did not become ready within 30s" >&2
exit 1
