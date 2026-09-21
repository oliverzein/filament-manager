#!/usr/bin/env bash
# release.sh [--push] <version> — tag, build the Docker image, and save a gzipped tarball.
# Optionally push tags to Docker Hub (default: w00z72/filament-manager).
#
#   scripts/release.sh 2.1.1
#   scripts/release.sh --push 2.1.1
#
# Requires a clean git tree. Produces dist/filament-manager-<version>.tar.gz
# and prints its sha256 checksum.
set -euo pipefail
cd "$(dirname "$0")/.."

push_hub=0
if [[ "${1:-}" == "--push" ]]; then
  push_hub=1
  shift
fi

if [[ $# -ne 1 ]]; then
  echo "usage: $0 [--push] <version>   (e.g. 2.1.1)" >&2
  exit 2
fi
version="$1"
docker_repo="${DOCKER_REPO:-w00z72/filament-manager}"
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
build_args=(-t "${image}" -t "filament-manager:latest")
if [[ "${push_hub}" -eq 1 ]]; then
  build_args+=(-t "${docker_repo}:${version}" -t "${docker_repo}:latest")
fi
docker build --network host "${build_args[@]}" .

if [[ "${push_hub}" -eq 1 ]]; then
  echo "==> pushing to Docker Hub (${docker_repo}:${version} and ${docker_repo}:latest)"
  docker push "${docker_repo}:${version}"
  docker push "${docker_repo}:latest"
fi

echo "==> saving ${tarball}"
mkdir -p dist
docker save "${image}" "filament-manager:latest" | gzip > "${tarball}"

echo "==> checksum"
sha256sum "${tarball}"
echo "release ${version} ready: ${tarball}"
