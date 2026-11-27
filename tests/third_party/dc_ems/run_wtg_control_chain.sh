#!/usr/bin/env bash
# DCEMS 真实 WTG FC16 专项。复用隔离 CLI 生命周期，不复用 PPC 运行目录。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export DCEMS_CONTROL_ROOT="${DCEMS_WTG_CONTROL_ROOT:-${DCEMS_CONTROL_ROOT:-${ROOT}/run/dcems-control-wtg}}"

case "${1:-}" in
    --prepare)
        python -m tests.third_party.dc_ems.prepare_wtg_control_chain \
            --output "${DCEMS_CONTROL_ROOT}" \
            --nats-url "${DCEMS_CONTROL_NATS_URL:-nats://127.0.0.1:14223}"
        ;;
    --check)
        bash tests/third_party/dc_ems/run_control_chain.sh --check
        python -m pytest -q \
            tests/third_party/dc_ems/test_dcems_control_output_contract.py \
            tests/third_party/dc_ems/test_wtg_control_chain_config.py \
            tests/third_party/dc_ems/test_wtg_dcems_seed.py
        ;;
    --up)
        bash tests/third_party/dc_ems/run_control_chain.sh --up
        # DCEMS WTG setpoint is a constant-power demand (mode=1). Mode=0
        # intentionally performs maximum-power tracking and ignores setpoint.
        # Apply only to this dedicated physical Generation through official CLI.
        generation_id="$(cat "${DCEMS_CONTROL_ROOT}/state/generation-id.txt")"
        python -m cli.main --config "${DCEMS_CONTROL_ROOT}/state/cli.yaml" \
            data-generator variables update \
            --generation-id "${generation_id}" \
            --device-id WTG_SIM01 \
            --request-id dcems-wtg-initialize-mode-one \
            --point-id control_mode --value 1 --yes
        ;;
    --down)
        bash tests/third_party/dc_ems/run_control_chain.sh --down
        ;;
    *)
        printf 'usage: %s --prepare|--check|--up|--down\n' "$0" >&2
        exit 2
        ;;
esac
