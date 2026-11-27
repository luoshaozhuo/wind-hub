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
export DEPLOY_CONFIG_DIR="${RUNTIME}/docker/config"
VERSION="$(sed -n 's/^release_version=//p' "${RELEASE}/VERSION")"
[[ -n "${VERSION}" ]] || fail "release version missing"
export SERVER_IMAGE="${SERVER_IMAGE:-dc-ems-server:${VERSION}}"

compose=(
    docker compose
    --project-name dcems-wtg-control
    --env-file "${RUNTIME}/docker/dcems.env"
    -f "${BASE}"
    -f "${OVERLAY}"
)
# DCEMS 发布镜像与 JAR 不可变；禁止测试覆盖 Java 字节码。
if [[ -n "${DCEMS_WTG_PATCHED_JAR:-}" ]]; then
    fail "DCEMS_WTG_PATCHED_JAR is unsupported: use the original DCEMS release image"
fi

cleanup_project() {
    # 资源缺失或 Compose 无法解析时，仅按项目标签清理容器与网络，绝不删除数据卷。
    local -a containers networks
    mapfile -t containers < <(docker ps -aq --filter label=com.docker.compose.project=dcems-wtg-control)
    if (( ${#containers[@]} > 0 )); then
        docker rm -f "${containers[@]}" >/dev/null
    fi
    mapfile -t networks < <(docker network ls -q --filter label=com.docker.compose.project=dcems-wtg-control)
    if (( ${#networks[@]} > 0 )); then
        docker network rm "${networks[@]}" >/dev/null 2>&1 || true
    fi
    printf 'DCEMS WTG Server: cleaned project containers/networks; volumes preserved.\n'
}

if [[ "$1" == --down ]]; then
    if [[ -f "${RUNTIME}/docker/dcems.env" ]] && "${compose[@]}" config --quiet >/dev/null 2>&1; then
        "${compose[@]}" down --remove-orphans
    else
        cleanup_project
    fi
    exit 0
fi

export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
python -m tests.third_party.dc_ems.preflight_wtg_dcems_server --root "${RUNTIME}" \
    || fail "T02 release prerequisites missing; see preflight report"

[[ -f "${DCEMS_WTG_SEED}" ]] || fail "prepare separate control data.json first"
[[ -f "${RUNTIME}/docker/dcems.env" ]] || fail "prepare isolated runtime first"
[[ -f "${DEPLOY_CONFIG_DIR}/db.property" ]] || fail "isolated database config missing"
grep -q '^DB_NAME=dcems_smoke$' "${RUNTIME}/docker/dcems.env" \
    || fail "unexpected database name"
grep -q '/dcems_smoke?' "${DEPLOY_CONFIG_DIR}/db.property" \
    || fail "unexpected database URL"
for img in "${SERVER_IMAGE}" "dc-ems-timescaledb:2.7.0-pg14"; do
    docker image inspect "${img}" >/dev/null 2>&1 || fail "missing local image: ${img}"
done

"${compose[@]}" config --quiet || fail "invalid Compose contract"

if [[ "$1" == --up ]]; then
    if docker volume inspect dcems-wtg-control_database >/dev/null 2>&1 &&
        [[ "${DCEMS_WTG_ALLOW_REUSE:-}" != 1 ]]; then
        fail "existing isolated DB volume; verify it before setting DCEMS_WTG_ALLOW_REUSE=1"
    fi
    "${compose[@]}" up -d --pull never
    printf 'WTG Control Web: https://127.0.0.1:18453/dc-ems/ (self-signed)\n'
    printf 'Server readiness and Docker-to-Gateway TCP must be verified independently.\n'
fi
