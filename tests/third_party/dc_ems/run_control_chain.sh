#!/usr/bin/env bash
# DCEMS 控制专项：使用正式 CLI 管理所有 wind-hub 业务进程。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export WIND_HUB_DEPLOYMENT_ID=dcems-control
export WIND_HUB_DEPLOYMENT_VERSION=1.0.0
unset WIND_HUB_CONFIGURATION_ENDPOINT WIND_HUB_CONFIGURATION_NATS_SERVERS

BASE="${DCEMS_CONTROL_ROOT:-${ROOT}/run/dcems-control}"
CONFIG="${BASE}/configs"
STATE="${BASE}/state"
CLI="${STATE}/cli.yaml"

fail() { printf 'DCEMS control: %s\n' "$*" >&2; exit 1; }
case "${1:-}" in
    --prepare)
        python -m tests.third_party.dc_ems.prepare_control_chain --output "${BASE}" \
            --nats-url "${DCEMS_CONTROL_NATS_URL:-nats://127.0.0.1:14223}"
        exit 0
        ;;
    --check)
        python -m pytest -q tests/third_party/dc_ems/test_control_chain_config.py
        [[ -f "${CLI}" ]] || fail "run --prepare first"
        exit 0
        ;;
    --up|--down) ;;
    *) fail "usage: $0 --prepare|--check|--up|--down" ;;
esac
[[ -f "${CLI}" ]] || fail "run --prepare first"

cli() { python -m cli.main --config "${CLI}" "$@"; }

if [[ "$1" == --down ]]; then
    if [[ -f "${STATE}/generation-id.txt" ]]; then
        cli data-generator stop "$(cat "${STATE}/generation-id.txt")" --yes || true
    fi
    cli service stop data-generator --yes || true
    cli service stop gateway-target --yes || true
    cli service stop commander --yes || true
    cli service stop gateway --yes || true
    exit 0
fi

required=(
    WIND_HUB_E2E_GATEWAY_OPERATOR_TOKEN
    WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN
    WIND_HUB_E2E_COMMANDER_EMS_TOKEN
    WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN
    WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN
    WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN
    WIND_HUB_E2E_GENERATOR_GATEWAY_TOKEN
    WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN
    WIND_HUB_E2E_GENERATOR_CLI_TOKEN
)
for name in "${required[@]}"; do
    value="${!name:-}"
    [[ "${#value}" -ge 32 ]] || fail "missing or short token: ${name}"
done
python - "${CONFIG}" <<'PY'
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit
import yaml

config = Path(sys.argv[1])
commander = yaml.safe_load(
    (config / "services/service_commander-dcems-control_v1.0.0.yaml").read_text()
)
servers = commander["audit"]["nats_servers"]
assert len(servers) == 1
url = urlsplit(servers[0])
assert url.scheme == "nats" and url.hostname == "127.0.0.1"
assert url.port and url.port not in {4222, 14222}
with socket.create_connection(("127.0.0.1", url.port), timeout=3):
    pass
PY

# 先启动接收模拟执行的 Gateway2，再启动 Commander/入口 Gateway1 和 DG。
cli service start gateway --yes
cli service start commander --yes
cli service start gateway-target --yes
cli service start data-generator --yes
started="$(cli data-generator start e2e-plant-physical --yes)"
printf '%s' "${started}" | python -c '
import json
import sys
from pathlib import Path
payload = json.load(sys.stdin)
generation_id = str(payload["data"]["generation_id"])
assert generation_id
Path(sys.argv[1]).write_text(generation_id, encoding="utf-8")
' "${STATE}/generation-id.txt"
printf 'Control services launched. Verify business readiness, then run DCEMS_CONTROL_LIVE=1 pytest.\n'
