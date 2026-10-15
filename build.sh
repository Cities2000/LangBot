#!/usr/bin/env bash
# Release flow for the custom LangBot image: build FIRST, then let compose
# recreate only the services whose image changed. Old containers keep serving
# during the build, and a failure leaves the previous deployment untouched.
#
# Usage:
#   ./build.sh              # normal release (uses docker build cache)
#   ./build.sh --no-cache   # full image rebuild
#
# Rollback after a bad release:
#   docker tag langbot-custom:previous langbot-custom
#   cd docker && docker compose up -d
set -euo pipefail

cd "$(dirname "$0")"

IMAGE=langbot-custom
PREVIOUS=langbot-custom:previous

BUILD_FLAGS=()
if [ "${1:-}" = "--no-cache" ]; then
  BUILD_FLAGS+=(--no-cache)
elif [ $# -gt 0 ]; then
  echo "usage: ./build.sh [--no-cache]" >&2
  exit 1
fi

REVISION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# 1. Tag the current image as the rollback target (skipped on first build).
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker tag "$IMAGE" "$PREVIOUS"
fi

# 2. Build the new image; running containers are not touched.
# ${ARR[@]+...} guards the empty-array expansion: plain "${ARR[@]}" under
# set -u fails on bash < 4.4 (e.g. CentOS 7) when no flags were given.
echo "==> building ${IMAGE} (revision ${REVISION})"
docker build ${BUILD_FLAGS[@]+"${BUILD_FLAGS[@]}"} \
  --build-arg "LANGBOT_BUILD_REVISION=${REVISION}" \
  -t "$IMAGE" .

# 3. Recreate compose services whose image changed (langbot, langbot_plugin_runtime).
#    Release customizations (custom image tag, plugin pip mirror) live in
#    docker/docker-compose.override.yaml, which `docker compose up` merges with
#    the upstream docker-compose.yaml automatically — keep the upstream file
#    untouched so master merges never conflict here.
#    Plugin/data volumes under docker/data are untouched, so installed plugins
#    and the database survive the release.
echo "==> recreating compose services"
(cd docker && docker compose up -d --remove-orphans)

# 4. Reclaim dangling layers from the replaced build. The :previous rollback
#    image is tagged, so it is never pruned here.
docker image prune -f >/dev/null

echo "==> release done (revision ${REVISION})"
echo "    rollback: docker tag ${PREVIOUS} ${IMAGE} && (cd docker && docker compose up -d)"
