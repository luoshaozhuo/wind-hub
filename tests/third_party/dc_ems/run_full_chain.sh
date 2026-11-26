#!/usr/bin/env bash
# 隔离测量闭环，所有 wind-hub 业务服务由正式 CLI 管理。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export WIND_HUB_DEPLOYMENT_ID=dcems-measurement
export WIND_HUB_DEPLOYMENT_VERSION=1.0.0
unset WIND_HUB_CONFIGURATION_ENDPOINT WIND_HUB_CONFIGURATION_NATS_SERVERS

BASE="${DCEMS_MEASUREMENT_ROOT:-${ROOT}/run/dcems-measurement}"
CONFIG="${BASE}/configs"
STATE="${BASE}/state"
CLI="${STATE}/cli.yaml"

fail() { printf 'DCEMS measurement: %s\n' "$*" >&2; exit 1; }
case "${1:-}" in
    --prepare)
        python -m tests.third_party.dc_ems.prepare_full_chain --output "${BASE}" \
            --nats-url "${DCEMS_MEASUREMENT_NATS_URL:-nats://127.0.0.1:14222}" \
            --read-merge-max-gap "${DCEMS_MEASUREMENT_READ_GAP:-0}" \
            --source-port-base "${DCEMS_MEASUREMENT_SOURCE_PORT_BASE:-15221}"
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
    cli service stop collector --yes || true
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
    WIND_HUB_COLLECTOR_ADMIN_TOKEN
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

# 进程拉起的瞬时 DEGRADED 不当作启动失败；只接受真实 RUNNING 的进程，
# 业务就绪（Collector Task / Sink Delivery）仍须由调用方单独核验。
start_service() {
    local name="$1" attempt output
    for attempt in 1 2 3; do
        if output="$(cli service start "${name}" --yes 2>&1)"; then
            printf '%s\n' "${output}"
            return 0
        fi
        if printf '%s' "${output}" | grep -q '"running": true'; then
            printf '%s\n' "${output}"
            return 0
        fi
        sleep 2
    done
    printf '%s\n' "${output:-service start failed: ${name}}" >&2
    return 1
}

start_service gateway
start_service gateway-target
start_service sink
start_service collector
start_service data-generator
for attempt in 1 2 3; do
    cli data-generator start dcems-random-ten --yes && break
    sleep 2
done
printf 'Started measurement services; verify Sink deliveries RUNNING before asserting data.\n'
