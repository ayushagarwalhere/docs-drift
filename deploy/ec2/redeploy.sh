#!/usr/bin/env bash
# Rebuild and restart the docs-drift web container on a single Docker host (EC2).
#
# Runs on the server. Builds Dockerfile.web at the requested commit, replaces the
# running container, waits for /healthz to report the new commit, and rolls back
# to the previous image if it does not become healthy.
#
#   DEPLOY_SHA=<commit> ./deploy/ec2/redeploy.sh
#
# Settings (environment, all optional except the origin):
#   APP_DIR                   checkout to build from         (default: this repo)
#   DEPLOY_SHA                commit or ref to deploy         (default: origin/main)
#   CONTAINER_NAME            running container name          (default: app; the proxy routes to it)
#   DOCKER_NETWORK            network shared with the proxy   (default: web)
#   HOST_BIND                 publish on host addr:port       (default: none; the proxy reaches it by name)
#   ENV_FILE                  container environment           (default: /etc/docs-drift.env, if present)
#   HEALTH_TIMEOUT            seconds to wait for health      (default: 90)
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DEPLOY_SHA="${DEPLOY_SHA:-origin/main}"
CONTAINER_NAME="${CONTAINER_NAME:-app}"
DOCKER_NETWORK="${DOCKER_NETWORK:-web}"
HOST_BIND="${HOST_BIND:-}"
ENV_FILE="${ENV_FILE:-/etc/docs-drift.env}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"

log() { printf '[redeploy] %s\n' "$*"; }

cd "$APP_DIR"
[[ -f "$ENV_FILE" ]] || { log "missing ${ENV_FILE} (needs at least DOCS_DRIFT_ALLOWED_ORIGIN=https://...)"; exit 2; }
docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 || docker network create "$DOCKER_NETWORK" >/dev/null
log "fetching ${DEPLOY_SHA} in ${APP_DIR}"
git fetch --quiet --tags origin main
git checkout --quiet --detach "$DEPLOY_SHA"
SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short=8 HEAD)"
LAST_TAG="$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || echo v0.0.0)"
VERSION="${LAST_TAG#v}.post0.dev0+g${SHORT}"
IMAGE="docs-drift-web:${SHORT}"

log "building ${IMAGE} (version ${VERSION})"
docker build --quiet -f Dockerfile.web \
  --build-arg BUILD_SHA="$SHA" --build-arg PACKAGE_VERSION="$VERSION" \
  -t "$IMAGE" -t docs-drift-web:latest .

PREVIOUS_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null || true)"

run_container() {
  local image="$1"
  local args=(
    run -d --name "$CONTAINER_NAME" --restart unless-stopped
    --network "$DOCKER_NETWORK"
    --memory=2g --cpus=1 --pids-limit=256
    --cap-drop=ALL --security-opt=no-new-privileges
    -v docs-drift-data:/data
  )
  [[ -n "$HOST_BIND" ]] && args+=(-p "${HOST_BIND}:8080")
  [[ -f "$ENV_FILE" ]] && args+=(--env-file "$ENV_FILE")
  docker "${args[@]}" "$image" >/dev/null
}

wait_healthy() {
  local expected="$1" deadline=$((SECONDS + HEALTH_TIMEOUT)) body
  while (( SECONDS < deadline )); do
    body="$(docker exec "$CONTAINER_NAME" python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/healthz', timeout=4).read().decode())" 2>/dev/null || true)"
    if [[ "$body" == *"\"ok\""* && ( -z "$expected" || "$body" == *"$expected"* ) ]]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

log "replacing container ${CONTAINER_NAME} (previous image: ${PREVIOUS_IMAGE:-none})"
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
run_container "$IMAGE"

if wait_healthy "$SHA"; then
  log "healthy at ${SHA}"
  docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true
  exit 0
fi

log "new container did not become healthy; recent logs:"
docker logs --tail 50 "$CONTAINER_NAME" || true
if [[ -n "$PREVIOUS_IMAGE" ]]; then
  log "rolling back to ${PREVIOUS_IMAGE}"
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  run_container "$PREVIOUS_IMAGE"
  wait_healthy "" && log "rollback healthy" || log "rollback also unhealthy"
fi
exit 1
