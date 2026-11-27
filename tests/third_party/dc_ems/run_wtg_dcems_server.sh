#!/usr/bin/env bash
# 单风机真实 DCEMS Server：独立 Compose、只用本地已存在的第三方镜像。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
HERE="${ROOT}/tests/third_party/dc_ems"
RELEASE="${ROOT}/third_party/dc-ems"
BASE="${RELEASE}/runtime/containerized/compose.yaml"
OVERLAY="${HERE}/compose.wtg_control.yaml"
RUNTIME="${DCEMS_WTG_CONTROL_ROOT:-${ROOT}/run/dcems-control-wtg}"

fail() { printf 'DCEMS WTG Server: %s\n' "$*" >&2; exit 1; }
[[ "${1:-}" == --check || "${1:-}" == --up || "${1:-}" == --down ]] \
    || fail "usage: $0 --check|--up|--down"
[[ "${DCEMS_ALLOW_LOCAL_SMOKE:-}" == "1" ]] \
    || fail "set DCEMS_ALLOW_LOCAL_SMOKE=1"
command -v docker >/dev/null || fail "Docker missing"
docker compose version >/dev/null || fail "Docker Compose missing"

export DCEMS_RELEASE_DIR="${RELEASE}"
export DCEMS_TEST_DIR="${HERE}"
export DCEMS_WTG_SEED="${RUNTIME}/data.json"
export DCEMS_WTG_WEB="${RUNTIME}/web"
export DEPLOY_CONFIG_DIR="${RUNTIME}/docker/config"
VERSION="$(sed -n 's/^release_version=//p' "${RELEASE}/VERSION")"
[[ -n "${VERSION}" ]] || fail "release version missing"
export SERVER_IMAGE="${SERVER_IMAGE:-dc-ems-server:${VERSION}}"
export WEB_IMAGE="${WEB_IMAGE:-dc-ems-web:${VERSION}}"

if [[ "$1" != --down ]]; then
    export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
    python -m tests.third_party.dc_ems.preflight_wtg_dcems_server --root "${RUNTIME}" \
        || fail "T02 release prerequisites missing; see preflight report"
fi

if [[ "$1" != --down ]]; then
[[ -f "${DCEMS_WTG_SEED}" ]] || fail "prepare separate control data.json first"
[[ -f "${DCEMS_WTG_WEB}/index.html" ]] || fail "prepare isolated Web assets first"
[[ -f "${RUNTIME}/docker/dcems.env" ]] || fail "prepare isolated runtime first"
[[ -f "${DEPLOY_CONFIG_DIR}/db.property" ]] || fail "isolated database config missing"
grep -q '^DB_NAME=dcems_smoke$' "${RUNTIME}/docker/dcems.env" \
    || fail "unexpected database name"
grep -q '/dcems_smoke?' "${DEPLOY_CONFIG_DIR}/db.property" \
    || fail "unexpected database URL"
for img in "${SERVER_IMAGE}" "${WEB_IMAGE}" "dc-ems-timescaledb:2.7.0-pg14"; do
    docker image inspect "${img}" >/dev/null 2>&1 || fail "missing local image: ${img}"
done
fi

compose=(
    docker compose
    --project-name dcems-wtg-control
    --env-file "${RUNTIME}/docker/dcems.env"
    -f "${BASE}"
    -f "${OVERLAY}"
)
"${compose[@]}" config --quiet || fail "invalid Compose contract"

if [[ "$1" == --up ]]; then
    if docker volume inspect dcems-wtg-control_database >/dev/null 2>&1 &&
        [[ "${DCEMS_WTG_ALLOW_REUSE:-}" != 1 ]]; then
        fail "existing isolated DB volume; verify it before setting DCEMS_WTG_ALLOW_REUSE=1"
    fi
    "${compose[@]}" up -d --pull never
    printf 'WTG Control Web: http://127.0.0.1:18090/dc-ems/\n'
    printf 'Server readiness and Docker-to-Gateway TCP must be verified independently.\n'
fi

if [[ "$1" == --down ]]; then
    "${compose[@]}" down --remove-orphans
fi
