#!/usr/bin/env bash
# DCEMS 隔离联测启动：只使用本地已有镜像，禁止隐式拉取。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "${HERE}/../../.." && pwd -P)"
RELEASE="${ROOT}/third_party/dc-ems"
BASE="${RELEASE}/runtime/containerized/compose.yaml"
OVERLAY="${HERE}/compose.web.yaml"

fail() { printf 'DCEMS smoke: %s\n' "$*" >&2; exit 1; }

[[ "${1:-}" == "--check" || "${1:-}" == "--up" ]] || fail "usage: $0 --check|--up"
[[ "${DCEMS_ALLOW_LOCAL_SMOKE:-}" == "1" ]] || fail "set DCEMS_ALLOW_LOCAL_SMOKE=1 for isolated local testing"
command -v docker >/dev/null 2>&1 || fail "Docker is unavailable"
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is unavailable"

export DCEMS_RELEASE_DIR="${RELEASE}"
export DCEMS_TEST_DIR="${HERE}"
export DEPLOY_CONFIG_DIR="${RELEASE}/config"
export DCEMS_TEST_SEED="${DCEMS_TEST_SEED:-}"
export DCEMS_TEST_WEB="${DCEMS_TEST_WEB:-${ROOT}/run/dcems-smoke/web}"
export SERVER_IMAGE="${SERVER_IMAGE:-dc-ems-java:8}"
export WEB_IMAGE="${WEB_IMAGE:-dc-ems-nginx:1.26-alpine}"

[[ "${DCEMS_TEST_SEED}" == /* && -f "${DCEMS_TEST_SEED}" ]] || fail "DCEMS_TEST_SEED must be an existing absolute path"
[[ -f "${DCEMS_TEST_WEB}/index.html" ]] || fail "run python -m tests.third_party.dc_ems.prepare_web first"
grep -q "location.protocol" "${DCEMS_TEST_WEB}/static/js/app.4ad853ce.js" || fail "WebSocket runtime patch is missing"
[[ -f "${RELEASE}/.env" ]] || fail "missing local third_party/dc-ems/.env"
[[ -f "${RELEASE}/config/db.property" ]] || fail "missing local config/db.property"
[[ -f "${RELEASE}/config/ssl/dc-ems.jks" ]] || fail "missing local config/ssl/dc-ems.jks"
[[ -f "${RELEASE}/config/log4j.properties" ]] || fail "missing log4j.properties"

for image in "${SERVER_IMAGE}" "${WEB_IMAGE}" "dc-ems-timescaledb:2.7.0-pg14"; do
    docker image inspect "${image}" >/dev/null 2>&1 || fail "missing local image: ${image}"
done

compose=(
    docker compose
    --project-name dcems-smoke
    --env-file "${RELEASE}/.env"
    -f "${BASE}"
    -f "${OVERLAY}"
)
"${compose[@]}" config --quiet || fail "compose contract invalid"
printf 'DCEMS smoke: preflight OK; image content and runtime not validated\n'

if [[ "$1" == "--up" ]]; then
    "${compose[@]}" up -d --pull never
    printf 'Web entry: http://127.0.0.1:18080/dc-ems/\n'
    printf 'Run: DCEMS_LIVE=1 pytest tests/third_party/dc_ems/test_live_smoke.py -q\n'
fi
