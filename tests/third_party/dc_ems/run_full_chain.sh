#!/usr/bin/env bash
# 隔离测量闭环，所有 wind-hub 业务服务由正式 CLI 管理。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export WIND_HUB_DEPLOYMENT_ID=dcems-measurement
export WIND_HUB_DEPLOYMENT_VERSION=1.0.0
unset WIND_HUB_CONFIGURATION_ENDPOINT WIND_HUB_CONFIGURATION_NATS_SERVERS

BASE="${ROOT}/run/dcems-measurement"
CONFIG="${BASE}/configs"
STATE="${BASE}/state"
CLI="${STATE}/cli.yaml"

fail() { printf 'DCEMS measurement: %s\n' "$*" >&2; exit 1; }
case "${1:-}" in
    --prepare)
        python -m tests.third_party.dc_ems.prepare_full_chain --output "${BASE}" \
            --nats-url "${DCEMS_MEASUREMENT_NATS_URL:-nats://127.0.0.1:14222}"
        python -m tests.third_party.dc_ems.prepare_full_chain_cli \
            --config-root "${CONFIG}" --runtime-dir "${STATE}"
        exit 0
        ;;
    --check|--up|--down) ;;
    *) fail "usage: $0 --prepare|--check|--up|--down" ;;
esac
[[ -f "${CLI}" ]] || fail "run --prepare before --check/--up/--down"

cli() { python -m cli.main --config "${CLI}" "$@"; }
if [[ "$1" == "--down" ]]; then
    cli service stop data-generator --yes || true
    cli service stop collector-realtime --yes || true
    cli service stop sink --yes || true
    cli service stop gateway-target --yes || true
    cli service stop gateway --yes || true
    exit 0
fi

required=(
    WIND_HUB_DCE_GENERATOR_GATEWAY_TOKEN
    WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN
    WIND_HUB_DCE_GENERATOR_CLI_TOKEN
    WIND_HUB_DCE_TARGET_OPERATOR_TOKEN
    WIND_HUB_DCE_TARGET_SINK_TOKEN
    WIND_HUB_SINK_CLI_TOKEN
)
for name in "${required[@]}"; do
    value="${!name:-}"
    [[ "${#value}" -ge 32 ]] || fail "missing or short auth token: ${name}"
done
[[ "${WIND_HUB_GATEWAY_SINK_TOKEN:-}" == "${WIND_HUB_DCE_TARGET_SINK_TOKEN}" ]] \
    || fail "WIND_HUB_GATEWAY_SINK_TOKEN must equal the target Sink token"

python -m industrial_gateway.main --config "${CONFIG}/services/service_gateway-dcems-source_v1.0.0.yaml" --project-root "${CONFIG}" --check
python -m industrial_gateway.main --config "${CONFIG}/services/service_gateway-dcems-target_v1.0.0.yaml" --project-root "${CONFIG}" --check
python -m data_generator.main --config "${CONFIG}/services/service_data-generator-dcems-measurement_v1.0.0.yaml" --check
python -m collector.main --config "${CONFIG}/services/service_collector-dcems-realtime_v2.0.0.yaml" --check

python -m tests.third_party.dc_ems.check_full_chain --root "${BASE}"
[[ "$1" == "--check" ]] && exit 0

# 从已生成的配置读取端口，只准连接本机独立 NATS。
python - "${CONFIG}" <<'PY'
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit
import yaml

config = Path(sys.argv[1])
collector = yaml.safe_load(
    (config / "services" / "service_collector-dcems-realtime_v2.0.0.yaml").read_text()
)
sink = yaml.safe_load(
    (config / "services" / "service_sink-dcems-measurement_v2.0.0.yaml").read_text()
)
source = collector["messaging"]["servers"]
target = sink["nats"]["servers"]
assert source == target and len(source) == 1, "NATS endpoints do not match"
uri = urlsplit(source[0])
assert uri.scheme == "nats" and uri.hostname == "127.0.0.1", "NATS must use loopback"
assert uri.port and uri.port != 4222, "dedicated nondefault NATS port required"
with socket.create_connection(("127.0.0.1", uri.port), timeout=3):
    pass
PY

cli service start gateway --yes
cli service start gateway-target --yes
cli service start sink --yes
cli service start collector-realtime --yes
cli service start data-generator --yes
cli data-generator start dcems-random-ten --yes
printf 'Started measurement services; verify Sink deliveries RUNNING before asserting data.\n'
