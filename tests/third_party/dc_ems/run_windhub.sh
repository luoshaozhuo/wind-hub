#!/usr/bin/env bash
# 通过正式 CLI 启动隔离的 Gateway 和 Data Generator random 场景。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export WIND_HUB_DEPLOYMENT_ID=dcems-smoke
export WIND_HUB_DEPLOYMENT_VERSION=1.0.0

CONFIG_ROOT="${ROOT}/run/dcems-smoke/configs"
STATE="${ROOT}/run/dcems-smoke/state"
CLI="${STATE}/cli.yaml"
GW="${CONFIG_ROOT}/services/service_gateway-dcems-smoke_v1.0.0.yaml"
DG="${CONFIG_ROOT}/services/service_data-generator-dcems-smoke_v1.0.0.yaml"

fail() { printf 'wind-hub DCEMS smoke: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--prepare" ]]; then
    python -m tests.third_party.dc_ems.stage_config --output "${CONFIG_ROOT}"
    python -m tests.third_party.dc_ems.prepare_cli \
        --config-root "${CONFIG_ROOT}" --runtime-dir "${STATE}"
    printf 'Prepared isolated configuration under %s\n' "${CONFIG_ROOT}"
    exit 0
fi

[[ -f "${CLI}" ]] || fail "run --prepare before --check/--up"
for env_name in WIND_HUB_DCE_GENERATOR_GATEWAY_TOKEN WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN WIND_HUB_DCE_GENERATOR_CLI_TOKEN; do
    value="${!env_name:-}"
    [[ "${#value}" -ge 32 ]] || fail "missing or short authentication token: ${env_name}"
done

python -m industrial_gateway.main --config "${GW}" --project-root "${CONFIG_ROOT}" --check
python -m data_generator.main --config "${DG}" --check

case "${1:-}" in
    --check)
        printf 'Gateway and Generator static startup checks passed\n'
        ;;
    --up)
        python -m cli.main --config "${CLI}" service start gateway --yes
        python -m cli.main --config "${CLI}" service start data-generator --yes
        python -m cli.main --config "${CLI}" data-generator start dcems-random-ten --yes
        ;;
    --down)
        python -m cli.main --config "${CLI}" service stop data-generator --yes || true
        python -m cli.main --config "${CLI}" service stop gateway --yes || true
        ;;
    *)
        fail "usage: $0 --prepare|--check|--up|--down"
        ;;
esac
