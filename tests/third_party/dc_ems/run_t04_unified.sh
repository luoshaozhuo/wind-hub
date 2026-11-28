#!/usr/bin/env bash
# T04 十机测量 + 单机控制统一联测编排：所有 wind-hub 服务由正式 CLI 管理。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export WIND_HUB_DEPLOYMENT_ID=dcems-t04
export WIND_HUB_DEPLOYMENT_VERSION=1.0.0
unset WIND_HUB_CONFIGURATION_ENDPOINT WIND_HUB_CONFIGURATION_NATS_SERVERS

BASE="${DCEMS_T04_ROOT:-${ROOT}/run/dcems-t04}"
CONFIG="${BASE}/configs"
STATE="${BASE}/state"
CLI="${STATE}/cli.yaml"
NATS_URL="${DCEMS_T04_NATS_URL:-nats://127.0.0.1:14222}"
PROBE_PID="${STATE}/modbus-probe.pid"

fail() { printf 'DCEMS T04: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    --prepare) ;;
    --check|--up|--down) ;;
    *) fail "usage: $0 --prepare|--check|--up|--down" ;;
esac

required_tokens=(
    WIND_HUB_DCE_GENERATOR_GATEWAY_TOKEN
    WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN
    WIND_HUB_DCE_GENERATOR_CLI_TOKEN
    WIND_HUB_DCE_TARGET_OPERATOR_TOKEN
    WIND_HUB_DCE_TARGET_SINK_TOKEN
    WIND_HUB_SINK_CLI_TOKEN
    WIND_HUB_COLLECTOR_ADMIN_TOKEN
    WIND_HUB_E2E_COMMANDER_EMS_TOKEN
    WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN
    WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN
    WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN
    WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN
)

if [[ "$1" == "--prepare" ]]; then
    [[ ! -e "${BASE}" ]] || fail "root exists: ${BASE} (use a fresh DCEMS_T04_ROOT)"
    command -v docker >/dev/null || fail "Docker missing"
    docker image inspect dc-ems-server:1.1.4-583d5da >/dev/null 2>&1 \
        || fail "missing dc-ems-server image"
    docker image inspect dc-ems-timescaledb:2.7.0-pg14 >/dev/null 2>&1 \
        || fail "missing timescaledb image"
    # 隔离 JetStream NATS 必须已由线下测试基础设施启动（14222）。
    python - "${NATS_URL}" <<'PY'
import asyncio, sys
from urllib.parse import urlsplit
import nats

async def main() -> None:
    url = urlsplit(sys.argv[1])
    assert url.scheme == "nats" and url.hostname == "127.0.0.1" and url.port != 4222
    nc = await nats.connect(sys.argv[1])
    js = nc.jetstream()
    try:
        await js.add_stream(
            name="COMMAND_AUDIT",
            subjects=["windhub.commander.audit"],
            storage="file",
            max_age=3600,
        )
        print("COMMAND_AUDIT created on", sys.argv[1])
    except Exception as exc:  # 已存在即视为就绪
        if "already in use" not in str(exc) and "10058" not in str(exc):
            raise
        print("COMMAND_AUDIT already present on", sys.argv[1])
    await nc.close()

asyncio.run(main())
PY

    python -m tests.third_party.dc_ems.prepare_t04_unified \
        --output "${BASE}" --nats-url "${NATS_URL}"

    # 发布配置副本 + 站点 JKS（镜像内 keytool；不写 third_party/）。
    mkdir -p "${BASE}/release-config/config/ssl"
    cp third_party/dc-ems/config/env.template "${BASE}/release-config/.env"
    cp third_party/dc-ems/config/log4j.properties \
        "${BASE}/release-config/config/log4j.properties"
    docker run --rm --user "$(id -u):$(id -g)" \
        --entrypoint /opt/java/openjdk/bin/keytool \
        -v "${BASE}/release-config/config/ssl:/ssl" \
        dc-ems-server:1.1.4-583d5da \
        -genkeypair -alias dc-ems -keyalg RSA -keysize 2048 -storetype JKS \
        -keystore /ssl/dc-ems.jks -storepass '1*@3789HZ-dcems' \
        -keypass '1*@3789HZ-dcems' -dname 'CN=DC-EMS' \
        -ext 'SAN=DNS:localhost,IP:127.0.0.1' -validity 365 -noprompt >/dev/null
    chmod 700 "${BASE}/release-config/config/ssl"
    chmod 600 "${BASE}/release-config/.env" \
        "${BASE}/release-config/config/ssl/dc-ems.jks" \
        "${BASE}/release-config/config/log4j.properties"

    python -m tests.third_party.dc_ems.prepare_runtime \
        --source "${BASE}/release-config" --output "${BASE}/server/docker"

    # 合法授权：发布 JAR 官方工具 + 固定 machine-id（SYSCODE DC-EMS）。
    docker run --rm --entrypoint /opt/java/openjdk/bin/java \
        -v "${BASE}/server/docker/config/machine-id:/etc/machine-id:ro" \
        dc-ems-server:1.1.4-583d5da \
        -cp dc-ems.jar cn.hedpy.scada.license.LiscenceUtil machine-code DC-EMS \
        > "${BASE}/server/machine-code.txt"
    docker run --rm --entrypoint /opt/java/openjdk/bin/java \
        dc-ems-server:1.1.4-583d5da \
        -cp dc-ems.jar cn.hedpy.scada.license.GenLicense \
        "$(cat "${BASE}/server/machine-code.txt")" 20 \
        > "${BASE}/server/license.raw"
    python - "${BASE}" <<'PY'
import re, sys
from pathlib import Path
base = Path(sys.argv[1])
raw = (base / "server" / "license.raw").read_text().strip()
match = re.search(r"[A-Za-z0-9+/=]{160,}$", raw)
if match is None or len(match.group(0)) != 172:
    raise SystemExit("GenLicense did not produce a 172-char license")
(base / "server" / "license.env").write_text(match.group(0) + "\n")
(base / "server" / "license.env").chmod(0o600)
(base / "server" / "license.raw").unlink()
env_path = base / "server" / "docker" / "dcems.env"
lines = env_path.read_text().splitlines()
token = match.group(0)
written = False
out = []
for line in lines:
    if line.startswith("LICENSE_CODE="):
        out.append("LICENSE_CODE=" + token)
        written = True
    else:
        out.append(line)
if not written:
    out.append("LICENSE_CODE=" + token)
env_path.write_text("\n".join(out) + "\n")
env_path.chmod(0o600)
print("license written to dcems.env")
PY

    DCEMS_LICENSE_CODE="$(cat "${BASE}/server/license.env")" \
        python -m tests.third_party.dc_ems.prepare_t04_dcems_seed \
        --output "${BASE}/server/data.json" \
        --gateway-host host.docker.internal --gateway-port 15321
    printf 'T04 prepare complete: %s\n' "${BASE}"
    exit 0
fi

[[ -f "${CLI}" ]] || fail "run --prepare before --check/--up/--down"
cli() { python -m cli.main --config "${CLI}" "$@"; }

if [[ "$1" == "--down" ]]; then
    if [[ -f "${PROBE_PID}" ]]; then
        kill "$(cat "${PROBE_PID}")" 2>/dev/null || true
        rm -f "${PROBE_PID}"
    fi
    DCEMS_ALLOW_LOCAL_SMOKE=1 DCEMS_COMPOSE_PROJECT=dcems-t04 \
        DCEMS_WTG_CONTROL_ROOT="${BASE}/server" \
        bash tests/third_party/dc_ems/run_wtg_dcems_server.sh --down || true
    cli service stop data-generator --yes || true
    cli service stop collector --yes || true
    cli service stop sink --yes || true
    cli service stop commander --yes || true
    cli service stop gateway-target --yes || true
    cli service stop gateway --yes || true
    exit 0
fi

declare -A seen=()
for name in "${required_tokens[@]}"; do
    value="${!name:-}"
    [[ "${#value}" -ge 32 ]] || fail "missing or short auth token: ${name}"
    [[ -z "${seen[$value]:-}" ]] || fail "duplicate token reference: ${name}"
    seen["$value"]="$name"
done
[[ "${WIND_HUB_GATEWAY_SINK_TOKEN:-}" == "${WIND_HUB_DCE_TARGET_SINK_TOKEN}" ]] \
    || fail "WIND_HUB_GATEWAY_SINK_TOKEN must equal the target Sink token"

python -m industrial_gateway.main \
    --config "${CONFIG}/services/service_gateway-dcems-source_v1.0.0.yaml" \
    --project-root "${CONFIG}" --check
python -m industrial_gateway.main \
    --config "${CONFIG}/services/service_gateway-dcems-target_v1.0.0.yaml" \
    --project-root "${CONFIG}" --check
python -m data_generator.main \
    --config "${CONFIG}/services/service_data-generator-dcems-t04_v1.0.0.yaml" --check
python -m collector.main \
    --config "${CONFIG}/services/service_collector-dcems-realtime_v2.0.0.yaml" --check
python -m pytest -q tests/third_party/dc_ems/test_t04_unified_config.py

python - "${NATS_URL}" <<'PY'
import asyncio, sys
import nats

async def main() -> None:
    nc = await nats.connect(sys.argv[1])
    info = await nc.jetstream().stream_info("COMMAND_AUDIT")
    print("COMMAND_AUDIT messages:", info.state.messages)
    await nc.close()

asyncio.run(main())
PY
[[ "$1" == "--check" ]] && exit 0

start_service() {
    local name="$1" attempt output
    for attempt in 1 2 3; do
        if output="$(cli service start "${name}" --yes 2>&1)"; then
            printf '%s\n' "${output}"
            return 0
        fi
        if printf '%s\n' "${output}" | grep -q '"running": true'; then
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
start_service commander
start_service data-generator
started=""
for attempt in 1 2 3; do
    if started="$(cli data-generator start t04-unified-ten --yes)"; then
        break
    fi
    sleep 2
done
[[ -n "${started}" ]] || fail "t04-unified-ten scenario failed to start"
printf '%s\n' "${started}" | python -c '
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
generation_id = str(payload["data"]["generation_id"])
assert generation_id
Path(sys.argv[1]).write_text(generation_id, encoding="utf-8")
' "${STATE}/generation-id.txt"
# 物理 WTG01：先设低功率恒定目标，再切模式1（避免默认满功率需求触发超速跳闸）。
GEN_ID="$(cat "${STATE}/generation-id.txt")"
cli data-generator variables update \
    --generation-id "${GEN_ID}" \
    --device-id WTG01 --request-id t04-wtg01-safe-setpoint \
    --point-id active_power_setpoint --value 50000 --yes \
    || fail "WTG01 safe setpoint init failed"
cli data-generator variables update \
    --generation-id "${GEN_ID}" \
    --device-id WTG01 --request-id t04-wtg01-mode-one \
    --point-id control_mode --value 1 --yes \
    || fail "WTG01 control_mode init failed"

# FC03/FC04/FC16 透明探针：DCEMS(15321-15330) → Gateway1(15121-15130)。
nohup python -m tests.third_party.dc_ems.modbus_request_probe \
    --listen-base 15321 --target-base 15121 --count 10 \
    --output "${STATE}/modbus-frames.jsonl" \
    > "${STATE}/probe.log" 2>&1 &
echo $! > "${PROBE_PID}"

DCEMS_ALLOW_LOCAL_SMOKE=1 DCEMS_COMPOSE_PROJECT=dcems-t04 \
    DCEMS_WTG_CONTROL_ROOT="${BASE}/server" \
    DCEMS_CONTROL_ROOT="${BASE}/server" \
    bash tests/third_party/dc_ems/run_wtg_dcems_server.sh --check
DCEMS_ALLOW_LOCAL_SMOKE=1 DCEMS_COMPOSE_PROJECT=dcems-t04 \
    DCEMS_WTG_CONTROL_ROOT="${BASE}/server" \
    DCEMS_CONTROL_ROOT="${BASE}/server" \
    bash tests/third_party/dc_ems/run_wtg_dcems_server.sh --up
printf 'T04 services launched; verify deliveries RUNNING and DCEMS comOk before live tests.\n'
