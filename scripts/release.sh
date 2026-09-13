#!/usr/bin/env bash
# release.sh <version> — tag, build the Docker image, and save a gzipped tarball.
#
#   scripts/release.sh 2.0.0
#
# Requires a clean git tree. Produces dist/filament-manager-<version>.tar.gz
# and prints its sha256 checksum.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <version>   (e.g. 2.0.0)" >&2
  exit 2
fi
version="$1"
tag="v${version}"
image="filament-manager:${version}"
tarball="dist/filament-manager-${version}.tar.gz"

# Refuse to release from a dirty tree.
if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: git tree is not clean; commit or stash first" >&2
  git status --short >&2
  exit 1
fi

echo "==> tagging ${tag}"
if git rev-parse "${tag}" >/dev/null 2>&1; then
  echo "tag ${tag} already exists; leaving in place"
else
  git tag "${tag}"
fi

echo "==> building image ${image}"
docker build --network host -t "${image}" -t "filament-manager:latest" .

echo "==> saving ${tarball}"
mkdir -p dist
docker save "${image}" "filament-manager:latest" | gzip > "${tarball}"

echo "==> checksum"
sha256sum "${tarball}"
echo "release ${version} ready: ${tarball}"
