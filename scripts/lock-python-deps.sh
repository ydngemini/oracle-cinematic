#!/usr/bin/env bash
# Compile backend/requirements.in into the hash-locked backend/requirements.txt
# (audit 35-01), in the same base image the backend Dockerfile builds from, so
# environment markers resolve for the Python that production runs.
set -euo pipefail
cd "$(dirname "$0")/../backend"
IMAGE="$(sed -n 's/^FROM \(python:[^ ]*\).*/\1/p' Dockerfile | head -1)"
[ -n "$IMAGE" ] || { echo "could not read the base image from backend/Dockerfile" >&2; exit 1; }
container="$(docker create -w /work --entrypoint sh "$IMAGE" -c \
  "pip install --no-cache-dir -q pip-tools==7.5.2 && PIP_NO_CACHE_DIR=1 pip-compile --quiet --cache-dir /tmp/pc \
   --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url \
   --output-file requirements.txt requirements.in")"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
docker cp requirements.in "$container":/work/
docker cp requirements.txt "$container":/work/ 2>/dev/null || true   # keeps unchanged pins
docker start -a "$container"
docker cp "$container":/work/requirements.txt ./requirements.txt
echo "backend/requirements.txt regenerated from requirements.in ($IMAGE)"
