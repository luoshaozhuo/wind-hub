#!/usr/bin/env bash
# 部署脚本公共库：所有使用 Compose 的脚本共享环境准备逻辑。
set -e
set -o pipefail

fail() {
    printf '错误：%s\n' "$1" >&2
    exit 1
}

trim() {
    local value="$1"
    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"
    printf '%s' "${value}"
}

# 容器运行时探测：支持 docker、nerdctl（containerd）、podman。
# 探测后全局可用 ${DOCKER}（CLI）和 run_compose（编排函数）。
DOCKER=""
detect_container_runtime() {
    if [[ -n "${DOCKER}" ]]; then
        return 0
    fi

    if command -v nerdctl >/dev/null 2>&1; then
        DOCKER="nerdctl"
        run_compose() { nerdctl compose "$@"; }
    elif command -v docker >/dev/null 2>&1; then
        DOCKER="docker"
        run_compose() { docker compose "$@"; }
    elif command -v podman >/dev/null 2>&1; then
        DOCKER="podman"
        if command -v podman-compose >/dev/null 2>&1; then
            run_compose() { podman-compose "$@"; }
        else
            run_compose() { podman compose "$@"; }
        fi
    else
        fail '未检测到容器运行时（docker/nerdctl/podman）'
    fi

    "${DOCKER}" version >/dev/null 2>&1 || fail "${DOCKER} 不可用"
    run_compose version >/dev/null 2>&1 || fail "${DOCKER} compose 不可用"

    readonly DOCKER
    export DOCKER
    export -f run_compose
}

require_compose() {
    detect_container_runtime
}

release_version() {
    grep '^release_version=' "${ROOT}/VERSION" | cut -d= -f2-
}

parse_env() {
    local path="${ROOT}/.env"
    local line key value
    local -A map=()

    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="$(trim "${line}")"
        [[ -n "${line}" ]] || continue
        [[ "${line}" != \#* ]] || continue
        [[ "${line}" == *'='* ]] || fail ".env 含无效行：${line}"
        key="$(trim "${line%%=*}")"
        value="$(trim "${line#*=}")"
        case "${key}" in
            DB_NAME|DB_USER|DB_PASSWORD|DB_HOST|DB_PORT|HTTP_PORT|HTTPS_PORT|SERVER_PORT|RMI_PORT|NODE_ROLE|JKS_SAN|PEER_HOST|LICENSE_CODE|REDIS_HOST|REDIS_PORT|REDIS_PASSWORD|REDIS_DB)
                [[ -z "${map[${key}]:-}" ]] || fail ".env 字段重复：${key}"
                map["${key}"]="${value}"
                ;;
            *)
                fail ".env 含未支持字段：${key}"
                ;;
        esac
    done < "${path}"

    for key in DB_NAME DB_USER DB_PASSWORD DB_HOST DB_PORT HTTP_PORT HTTPS_PORT SERVER_PORT RMI_PORT NODE_ROLE JKS_SAN; do
        [[ -n "${map[${key}]:-}" ]] || fail ".env 缺少 ${key}"
        export "${key}=${map[${key}]}"
    done
    case "${map[NODE_ROLE]}" in
        primary|standby) ;;
        *) fail "NODE_ROLE 必须为 primary 或 standby，当前为：${map[NODE_ROLE]}" ;;
    esac
    # LICENSE_CODE / REDIS_* / PEER_HOST 可选
    if [[ -n "${map[LICENSE_CODE]:-}" ]]; then
        export LICENSE_CODE="${map[LICENSE_CODE]}"
    fi
    if [[ -n "${map[PEER_HOST]:-}" ]]; then
        export PEER_HOST="${map[PEER_HOST]}"
    fi
    for key in REDIS_HOST REDIS_PORT REDIS_PASSWORD REDIS_DB; do
        if [[ -n "${map[${key}]:-}" ]]; then
            export "${key}=${map[${key}]}"
        fi
    done
}

prepare_compose_env() {
    local version

    [[ -f "${ROOT}/VERSION" ]] ||
        fail '缺少 VERSION'

    version="$(
        awk -F= '/^release_version=/{print $2; exit}' \
            "${ROOT}/VERSION"
    )"

    [[ -n "${version}" ]] ||
        fail 'VERSION 中缺少 release_version'

    export SERVER_IMAGE="dc-ems-server:${version}"
    export WEB_IMAGE="dc-ems-web:${version}"
    export DEPLOY_CONFIG_DIR="${ROOT}/config"
}