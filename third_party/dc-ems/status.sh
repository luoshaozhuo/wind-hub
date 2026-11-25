#!/usr/bin/env bash
set -e
set -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly ROOT
source "${ROOT}/lib.sh"

[[ -f "${ROOT}/.deploy-mode" ]] || fail '尚未部署，请先执行 ./deploy.sh --mode ...'

mode="$(awk -F= '/^mode=/{print $2; exit}' "${ROOT}/.deploy-mode")"
case "${mode}" in
    containerized)
        require_compose
        parse_env
        prepare_compose_env
        run_compose --project-name dc-ems --project-directory "${ROOT}" --env-file /dev/null -f "${ROOT}/runtime/containerized/compose.yaml" ps
        ;;
    host-based)
        systemctl status dc-ems
        ;;
    *)
        fail '错误：.deploy-mode 无效'
        ;;
esac