"""双 Gateway 与 Collector/Sink 的真实协议测量端到端验收。"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pymodbus.client import AsyncModbusTcpClient

from tests.third_party.dc_ems.check_full_chain import POINTS
from tests.third_party.dc_ems.prepare_full_chain import STATION_POINTS

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CLI = ROOT / "run" / "dcems-measurement" / "state" / "cli.yaml"
DEFAULT_CONFIG = ROOT / "run" / "dcems-measurement" / "configs"


def _cli(*args: str) -> dict[str, Any]:
    """通过正式 CLI 查询生产服务的业务状态，不用配置推断替代运行事实。"""
    cli = Path(os.environ.get("DCEMS_MEASUREMENT_CLI", str(DEFAULT_CLI)))
    environment = {
        **os.environ,
        "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
        "WIND_HUB_DEPLOYMENT_ID": "dcems-measurement",
        "WIND_HUB_DEPLOYMENT_VERSION": "1.0.0",
    }
    # 令牌只从本机隔离运行目录注入，不进入源码或证据；调用方也可在
    # 当前 Shell 预先导出，已有环境变量优先。
    token_file = Path(
        os.environ.get(
            "DCEMS_MEASUREMENT_TOKEN_FILE",
            str(ROOT / "run" / "dcems-measurement" / "tokens.env"),
        )
    )
    if token_file.is_file():
        for line in token_file.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name and value and name not in os.environ:
                environment[name] = value
    completed = subprocess.run(
        [sys.executable, "-m", "cli.main", "--config", str(cli), *args],
        capture_output=True,
        text=True,
        timeout=90,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, f"{args}: {completed.stdout[-400:]} {completed.stderr[-400:]}"
    return cast(dict[str, Any], json.loads(completed.stdout))


def _identity_expectations() -> dict[str, dict[str, float]]:
    """读取隔离场景声明的每台常量，不把期望值硬编码进测试。"""
    config = Path(os.environ.get("DCEMS_MEASUREMENT_CONFIG_ROOT", str(DEFAULT_CONFIG)))
    scenarios = yaml.safe_load(
        (config / "instances" / "scenarios_dcems-smoke_v1.0.0.yaml").read_text(encoding="utf-8")
    )["scenarios"]
    identity = next(item for item in scenarios if item["scenario_id"] == "dcems-identity-ten")
    expected: dict[str, dict[str, float]] = {}
    for device in identity["devices"]:
        rules = identity["models"][device["model_ref"]]["rules"]
        expected[device["device_id"]] = {
            name: float(rules[name]["value"])
            for name in ("wind_speed", "sim_active_power", "sim_reactive_power")
        }
        if "sim_plant_active_power" in device["points"]:
            expected[device["device_id"]]["sim_plant_active_power"] = float(
                rules["sim_plant_active_power"]["value"]
            )
            expected[device["device_id"]]["sim_plant_reactive_power"] = float(
                rules["sim_plant_reactive_power"]["value"]
            )
    return expected


@pytest.fixture(scope="module", autouse=True)
def live_only() -> None:
    """只有显式启用真实链路，才读取独立运行中的协议端口。"""
    if os.environ.get("DCEMS_MEASUREMENT_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_MEASUREMENT_LIVE=1 required for real measurement")


def _decode_float(registers: list[int]) -> float:
    """按独立点表约定将 F32 ABCD 解码为浮点数。"""
    return float(struct.unpack(">f", struct.pack(">HH", *registers))[0])


async def _read_points(client: AsyncModbusTcpClient, first: bool) -> dict[str, float | bool]:
    """从实际 TCP 连接读取动态 P/Q、风速与安全状态。"""
    measurements: dict[str, float | bool] = {}
    offsets = {
        "wind_speed": 0,
        "sim_active_power": 8,
        "sim_reactive_power": 10,
    }
    if first:
        offsets.update(
            {
                "sim_plant_active_power": 2,
                "sim_plant_reactive_power": 4,
            }
        )
    for name, offset in offsets.items():
        response = await client.read_input_registers(offset, count=2, device_id=1)
        assert not response.isError(), f"{name}: Modbus read failure {response}"
        measurements[name] = _decode_float(response.registers)
    if first:
        mode = await client.read_discrete_inputs(0, count=1, device_id=1)
        assert not mode.isError()
        measurements["primary_frequency_mode"] = bool(mode.bits[0])
    return measurements


@pytest.mark.asyncio
async def test_source_to_target_ten_turbine_realtime() -> None:
    """逐机验证真实源端到目标端交付，不能仅以 Source 动态判为成功。"""
    host = os.environ.get("DCEMS_MEASUREMENT_HOST", "127.0.0.1")
    source_clients = [
        AsyncModbusTcpClient(host, port=15221 + index, timeout=3) for index in range(10)
    ]
    target_clients = [
        AsyncModbusTcpClient(host, port=15121 + index, timeout=3) for index in range(10)
    ]
    records: dict[str, dict[str, dict[str, list[float | bool]]]] = {}
    try:
        connections = await asyncio.gather(
            *(client.connect() for client in [*source_clients, *target_clients])
        )
        assert all(connections), "source or target Gateway listener unavailable"
        for index in range(10):
            device_id = f"WTG{index + 1:02d}"
            points = POINTS | STATION_POINTS if index == 0 else POINTS
            records[device_id] = {
                name: {"source": [], "target": []}
                for name in points
                if name
                in {
                    "wind_speed",
                    "sim_active_power",
                    "sim_reactive_power",
                    "sim_plant_active_power",
                    "sim_plant_reactive_power",
                    "primary_frequency_mode",
                }
            }
        for _ in range(45):
            for index in range(10):
                device_id = f"WTG{index + 1:02d}"
                source_sample = await _read_points(source_clients[index], index == 0)
                target_sample = await _read_points(target_clients[index], index == 0)
                for point, history in records[device_id].items():
                    history["source"].append(source_sample[point])
                    history["target"].append(target_sample[point])
            await asyncio.sleep(0.25)
    finally:
        for client in [*source_clients, *target_clients]:
            client.close()

    failures: list[str] = []
    for device_id, point_records in records.items():
        for point, history in point_records.items():
            source_history = history["source"]
            target_history = history["target"]
            if point == "primary_frequency_mode":
                if source_history != [False] * len(source_history) or target_history != [
                    False
                ] * len(target_history):
                    failures.append(f"{device_id}/{point}: unsafe mode")
                continue
            source_values = {round(float(item), 1) for item in source_history}
            target_values = {round(float(item), 1) for item in target_history}
            if len(source_values) < 2 or len(target_values) < 2:
                failures.append(f"{device_id}/{point}: frozen")
            if not source_values.intersection(target_values):
                failures.append(f"{device_id}/{point}: values never reconciled")
    evidence_dir = Path(
        os.environ.get("DCEMS_MEASUREMENT_EVIDENCE_DIR", "run/dcems-measurement/evidence")
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "source-target-reconciliation.json").write_text(
        json.dumps(
            {"failures": failures, "records": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert not failures, failures


@pytest.mark.asyncio
async def test_source_to_target_static_device_contract() -> None:
    """逐机核查状态、额定值、故障位在目标端的真实协议读回。"""
    host = os.environ.get("DCEMS_MEASUREMENT_HOST", "127.0.0.1")
    for index in range(10):
        source = AsyncModbusTcpClient(host, port=15221 + index, timeout=3)
        target = AsyncModbusTcpClient(host, port=15121 + index, timeout=3)
        try:
            assert await source.connect()
            assert await target.connect()
            for client in (source, target):
                mode = await client.read_input_registers(6, count=2, device_id=1)
                limit = await client.read_input_registers(12, count=2, device_id=1)
                rpm = await client.read_input_registers(14, count=2, device_id=1)
                pitch = await client.read_input_registers(16, count=2, device_id=1)
                minimum = await client.read_input_registers(18, count=2, device_id=1)
                fault = await client.read_discrete_inputs(1, count=1, device_id=1)
                assert all(
                    not result.isError() for result in (mode, limit, rpm, pitch, minimum, fault)
                ), f"WTG{index + 1:02d}: static point read failed"
                assert struct.unpack(">i", struct.pack(">HH", *mode.registers))[0] == 32
                assert abs(_decode_float(limit.registers) - 2000.0) < 0.001
                assert abs(_decode_float(rpm.registers) - 1500.0) < 0.001
                assert abs(_decode_float(pitch.registers) - 5.0) < 0.001
                assert abs(_decode_float(minimum.registers)) < 0.001
                assert fault.bits[0] is False
        finally:
            source.close()
            target.close()


def test_collector_task_running_and_publishing_to_nats() -> None:
    """Collector Task 必须 RUNNING，且真实 NATS 通道有成功发布。"""
    task = _cli("collector", "task", "dcems_source_all")["data"]["task"]
    assert task["desired_state"] == "START"
    assert task["actual_state"] == "RUNNING", task
    assert not task["last_error"], task["last_error"]
    channels = _cli("collector", "publishing")["data"]["channels"]
    realtime = [item for item in channels if item["channel"] == "realtime"]
    assert len(realtime) == 1, channels
    assert realtime[0]["nats_connected"] is True
    assert int(realtime[0]["published"]) > 0
    assert int(realtime[0]["failed"]) == 0, realtime[0]


def test_sink_ten_deliveries_are_running() -> None:
    """十条 Delivery 必须逐条实际 RUNNING 且无错误。"""
    deliveries = _cli("sink", "deliveries")["data"]["deliveries"]
    expected = {f"dcems_wtg{index:02d}" for index in range(1, 11)}
    assert {item["delivery_id"] for item in deliveries} == expected
    assert all(item["actual_state"] == "RUNNING" for item in deliveries), deliveries
    assert all(not item.get("last_error") for item in deliveries), deliveries


def test_collector_endpoints_exclude_target_gateway() -> None:
    """Collector 只采源端；目标端地址出现即说明存在回环采集风险。"""
    devices = _cli("collector", "devices")["data"]["devices"]
    endpoints = {int(item["endpoint"].split(":")[1]) for item in devices}
    assert endpoints == set(range(15221, 15231))
    assert all(item["actual_state"] == "RUNNING" for item in devices), devices
    assert not endpoints.intersection(range(15121, 15131))


def test_wrong_bearer_token_cannot_access_target_gateway() -> None:
    """Gateway1 注入面必须拒绝错误 Bearer，不得凭端点可达判定授权成功。"""
    import grpc

    from contracts import industrial_gateway_pb2 as gateway  # type: ignore[import-untyped]
    from contracts import industrial_gateway_pb2_grpc as gateway_rpc

    bad_token = "wrong-" + "x" * 40
    channel = grpc.insecure_channel("127.0.0.1:52075")
    stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
    try:
        with pytest.raises(grpc.RpcError) as raised:
            stub.ListVariables(
                gateway.ListVariablesRequest(gateway_id="dcems-target-01", page_size=10),
                metadata=(("authorization", f"Bearer {bad_token}"),),
                timeout=5,
            )
        assert raised.value.code() in {
            grpc.StatusCode.UNAUTHENTICATED,
            grpc.StatusCode.PERMISSION_DENIED,
        }, raised.value
    finally:
        channel.close()


def test_unauthorized_client_cannot_query_sink() -> None:
    """Sink 管理面必须拒绝错误 Bearer，防止未授权取得目标租约。"""
    import grpc

    from contracts import sink_service_pb2 as sink
    from contracts import sink_service_pb2_grpc as sink_rpc

    bad_token = "wrong-" + "y" * 40
    channel = grpc.insecure_channel("127.0.0.1:52053")
    stub = sink_rpc.SinkServiceStub(channel)
    try:
        with pytest.raises(grpc.RpcError) as raised:
            stub.ListDeliveries(
                sink.ListDeliveriesRequest(page_size=10),
                metadata=(("authorization", f"Bearer {bad_token}"),),
                timeout=5,
            )
        assert raised.value.code() in {
            grpc.StatusCode.UNAUTHENTICATED,
            grpc.StatusCode.PERMISSION_DENIED,
        }, raised.value
    finally:
        channel.close()


@pytest.mark.parametrize("index", range(10))
def test_collector_and_sink_share_verified_sample_identity(index: int) -> None:
    """跨服务证据必须共享同一真实 sample ID，仅数值交集不算因果。"""
    device_id = f"WTG{index + 1:02d}"
    delivery_id = f"dcems_wtg{index + 1:02d}"
    report = _cli("diagnose", "pipeline", device_id, "--sink-id", delivery_id)
    data = report["data"]
    assert data["overall_status"] == "PASS", data.get("limitations")
    assert data["collector"]["overall_status"] == "PASS"
    assert data["sink"]["overall_status"] == "SUCCESS"


def test_identity_stimulus_isolates_all_ten_devices() -> None:
    """独立常量激励逐台核对目标端身份，并证明站控点未被复制到其他风机。"""
    if os.environ.get("DCEMS_MEASUREMENT_ISOLATION") != "1":
        pytest.skip("NOT_RUN: start dcems-identity-ten before isolation acceptance")
    expected = _identity_expectations()
    assert len(expected) == 10
    assert len({values["wind_speed"] for values in expected.values()}) == 10
    assert len({values["sim_active_power"] for values in expected.values()}) == 10

    async def check() -> list[str]:
        failures: list[str] = []
        host = os.environ.get("DCEMS_MEASUREMENT_HOST", "127.0.0.1")
        source_clients = [
            AsyncModbusTcpClient(host, port=15221 + index, timeout=3) for index in range(10)
        ]
        target_clients = [
            AsyncModbusTcpClient(host, port=15121 + index, timeout=3) for index in range(10)
        ]
        try:
            connected = await asyncio.gather(
                *(client.connect() for client in [*source_clients, *target_clients])
            )
            assert all(connected), "source or target Gateway listener unavailable"
            for index in range(10):
                device_id = f"WTG{index + 1:02d}"
                wants = expected[device_id]
                for role, clients in (("source", source_clients), ("target", target_clients)):
                    for _ in range(3):
                        sources = await _read_points(clients[index], index == 0)
                        for point, wanted in wants.items():
                            actual = float(sources[point])
                            if abs(actual - wanted) > 1.0:
                                failures.append(f"{role}/{device_id}/{point}: {actual} != {wanted}")
                        await asyncio.sleep(0.5)
                # 站控/PFR 只属于 WTG01；其他设备地址必须不可读，不能有泄漏。
                if index > 0:
                    for client in (source_clients[index], target_clients[index]):
                        plant = await client.read_input_registers(2, count=2, device_id=1)
                        mode = await client.read_discrete_inputs(0, count=1, device_id=1)
                        if not plant.isError() or not mode.isError():
                            failures.append(f"{device_id}: WTG01 station/PFR leaked")
        finally:
            for client in [*source_clients, *target_clients]:
                client.close()
        return failures

    failures = asyncio.run(check())
    assert not failures, failures
