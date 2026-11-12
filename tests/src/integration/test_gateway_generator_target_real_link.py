"""S3 真实链路：Modbus 控制写 → Gateway 每 Server data_generator 目标
→ mTLS gRPC → Data Generator 服务（脚本化正式契约 Servicer）。

覆盖分支新增、其余套件零覆盖的 ``ControlTarget(kind="data_generator")`` 路径：
- 完整 GetDevice/GetGeneration/ListVariables/UpdateVariables 裁决链 ACCEPTED；
- 模型服务拒绝（accepted=False）→ REJECTED，Modbus 侧不闭锁后续写；
- Generation 失活 / 点不可写 → REJECTED（ValueError/PermissionError 确定语义）；
- mTLS 信任链失败 → UNKNOWN，且无自动重试；
- 同目标 FIFO：max_inflight=1 下两次写在服务端按入队顺序串行到达；
- 传输歧义不呈现为 ACCEPTED：effective_step<=0 → UNKNOWN。

PKI 在 tmp_path 独立生成，私钥仅存在于临时目录，不入库。
"""

from __future__ import annotations

import asyncio
import shutil
import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml
from pymodbus.client import AsyncModbusTcpClient

from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as ipb
from contracts import industrial_gateway_pb2_grpc as irpc
from contracts.scalar_codec import decode_scalar, encode_scalar
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.commander.forwarder import ForwardState
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots
from industrial_gateway.infrastructure.config.v1_resolution import load_gateway_v1
from industrial_gateway.infrastructure.grpc.data_server import create_gateway_grpc_server
from tests.support.gateway_v1 import (
    COMMAND_FLOAT,
    COMMAND_UINT,
    FEEDBACK_FLOAT,
    POINT_TABLE,
    float_words,
    reserve_port,
    write_mtls_pki,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.modbus,
]

SCENARIO_ID = "wind-plant-physical-acceptance"
GENERATION_ID = "gen-0001"
DEVICE_ID = "WTG_SIM01"
SERVER_ID = "S1"
ADDR = {name: spec["address"]["address"] for name, spec in POINT_TABLE.items()}


class FakeGeneratorServicer(dg_rpc.DataGeneratorServiceServicer):
    """正式 DG 契约的脚本化对端：只实现 Gateway 转发链实际调用的四个 RPC。"""

    def __init__(self, *, scenario_id: str, writable_points: Sequence[str]) -> None:
        self.scenario_id = scenario_id
        self.writable_points = tuple(writable_points)
        self.mode = "accept"  # accept | reject | stale | not_writable | zero_step
        self.update_log: list[dict[str, Any]] = []
        self.request_order: list[str] = []

    async def GetDevice(self, request, context):  # noqa: ANN001, ANN202, N802
        state = "STOPPED" if self.mode == "stale" else "RUNNING"
        generation = "" if self.mode == "stale" else GENERATION_ID
        return dg.GetDeviceResponse(
            device=dg.Device(
                device_id=request.id,
                protocol="modbus",
                endpoint="127.0.0.1:0",
                generation_id=generation,
                actual_state=state,
            )
        )

    async def GetGeneration(self, request, context):  # noqa: ANN001, ANN202, N802
        if self.mode == "stale":
            return dg.GetGenerationResponse(
                generation=dg.Generation(
                    generation_id=request.id,
                    scenario_id=self.scenario_id,
                    desired_state="STOPPED",
                    actual_state="STOPPED",
                )
            )
        return dg.GetGenerationResponse(
            generation=dg.Generation(
                generation_id=GENERATION_ID,
                scenario_id=self.scenario_id,
                desired_state="RUNNING",
                actual_state="RUNNING",
            )
        )

    async def ListVariables(self, request, context):  # noqa: ANN001, ANN202, N802
        writable = self.mode != "not_writable"
        return dg.ListVariablesResponse(
            variables=[
                dg.ModelVariable(
                    point_id=point,
                    business_point_id=point,
                    data_type="float32",
                    unit="watt",
                    role="INPUT",
                    writable=writable,
                )
                for point in self.writable_points
            ],
            generation_state="STOPPED" if self.mode == "stale" else "RUNNING",
        )

    async def UpdateVariables(self, request, context):  # noqa: ANN001, ANN202, N802
        self.request_order.append(request.request_id)
        entry = {
            "request_id": request.request_id,
            "generation_id": request.generation_id,
            "device_id": request.device_id,
            "values": [(item.point_id, decode_scalar(item.value)) for item in request.values],
        }
        self.update_log.append(entry)
        if self.mode == "reject":
            return dg.UpdateVariablesResponse(
                accepted=False,
                error_code="MODEL_OUT_OF_RANGE",
                message="model rejected the command",
                request_id=request.request_id,
            )
        if self.mode == "zero_step":
            return dg.UpdateVariablesResponse(
                accepted=True,
                effective_step=0,
                request_id=request.request_id,
            )
        return dg.UpdateVariablesResponse(
            accepted=True,
            effective_step=42,
            request_id=request.request_id,
        )


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_generator_tree(
    root: Path,
    *,
    modbus_port: int,
    dg_endpoint: str,
    scenario_id: str,
    cert_stem: str = "gateway-dg",
) -> Path:
    """写出 data_generator 控制目标的完整 v1 配置树（结构对齐生产 gateway_v1 助手）。"""
    _dump(
        root / "instances" / "devices.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "devices": {
                DEVICE_ID: {
                    "name": "S3 物理仿真风机",
                    "enabled": True,
                    "model": {"id": "gw_turbine", "version": "1.0.0"},
                    "endpoint": {"host": "127.0.0.1", "port": modbus_port},
                    "overrides": {"protocol_options": {"slave_id": 1}},
                }
            },
        },
    )
    _dump(
        root / "instances" / "site.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "site_id": "s3-generator-target-site",
            "name": "S3 测试现场",
        },
    )
    _dump(
        root / "templates" / "units.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "quantities": {"dimensionless": {"base_unit": "1"}},
            "units": {"1": {"symbol": "1", "quantity": "dimensionless"}},
        },
    )
    _dump(
        root / "templates" / "business_points.yaml",
        {"schema_version": 1, "file_version": "1.0.0", "business_points": {}},
    )
    _dump(
        root / "templates" / "protocols" / "gw_modbus_1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "gw_modbus",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "protocol_options": {
                "mode": "tcp",
                "slave_id": 1,
                "timeout": 5.0,
                "word_order": "big_endian",
                "health_point_id": None,
            },
        },
    )
    _dump(
        root / "templates" / "device_models" / "gw_turbine_1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "gw_turbine",
            "file_version": "1.0.0",
            "name": "S3 测试机型",
            "device_type_id": "turbine",
            "protocol": {"id": "gw_modbus", "version": "1.0.0"},
            "point_table": {"id": "gw_points", "version": "1.0.0"},
        },
    )
    _dump(
        root / "templates" / "point_tables" / "gw_points_1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "gw_points",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "points": {key: dict(value) for key, value in POINT_TABLE.items()},
        },
    )
    grpc_port = reserve_port()
    service = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "config_sources": {
            "instances": {"devices": "instances/devices.yaml"},
            "templates": {"root": "templates"},
        },
        "grpc": {"listen_host": "127.0.0.1", "listen_port": grpc_port},
        "servers": [
            {
                "server_id": SERVER_ID,
                "device_id": DEVICE_ID,
                "listen_host": "127.0.0.1",
                "listen_port": modbus_port,
                "desired_state": "START",
                "control": {
                    "enabled": True,
                    "target": {
                        "kind": "data_generator",
                        "endpoint": dg_endpoint,
                        "tls": {
                            "enabled": True,
                            "cert_file": f"certs/client-{cert_stem}.crt",
                            "key_file": f"certs/client-{cert_stem}.key",
                            "ca_file": "certs/ca.crt",
                        },
                        "queue_capacity": 1024,
                        "max_inflight": 1,
                        "request_timeout_ms": 5000,
                        "generation_binding": {"scenario_id": scenario_id},
                    },
                    "mappings": [
                        {"variable_id": COMMAND_FLOAT, "target_point_id": COMMAND_FLOAT},
                        {"variable_id": COMMAND_UINT, "target_point_id": COMMAND_UINT},
                    ],
                },
            }
        ],
        "metrics": {"window_seconds": 60, "collect_interval_ms": 1000},
    }
    service_path = root / "service.yaml"
    _dump(service_path, service)
    return service_path, grpc_port


async def _start_generator_server(
    root: Path, pki: dict[str, object], servicer: FakeGeneratorServicer
) -> tuple[grpc.aio.Server, int]:
    port = reserve_port()
    server = grpc.aio.server()
    dg_rpc.add_DataGeneratorServiceServicer_to_server(servicer, server)
    ca = Path(pki["ca"]).read_bytes()  # type: ignore[arg-type]
    server_crt = Path(pki["server_crt"]).read_bytes()  # type: ignore[arg-type]
    server_key = Path(pki["server_key"]).read_bytes()  # type: ignore[arg-type]
    credentials = grpc.ssl_server_credentials(
        [(server_key, server_crt)],
        root_certificates=ca,
        require_client_auth=True,
    )
    server.add_secure_port(f"127.0.0.1:{port}", credentials)
    await server.start()
    return server, port


async def _wait_terminal(runtime, point_id: str, value: float) -> Any:
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        for record in runtime.forwarders[SERVER_ID].records:
            if (
                record.point_id == point_id
                and record.value == value
                and record.state
                in {
                    ForwardState.ACCEPTED,
                    ForwardState.REJECTED,
                    ForwardState.UNKNOWN,
                    ForwardState.OVERLOADED,
                }
            ):
                return record
        await asyncio.sleep(0.02)
    raise AssertionError(f"terminal forward record for {point_id}={value} not observed")


async def _start_runtime(root: Path, service_path: Path):
    config = load_gateway_v1(service_path, project_root=root)
    runtime = assemble_gateway(config)
    await runtime.start()
    return runtime


@pytest.fixture
async def linked_harness(tmp_path: Path):
    root = tmp_path / "gw"
    root.mkdir(parents=True)
    pki = write_mtls_pki(root, ("gateway-dg",))
    servicer = FakeGeneratorServicer(
        scenario_id=SCENARIO_ID, writable_points=(COMMAND_FLOAT, COMMAND_UINT)
    )
    server, dg_port = await _start_generator_server(root, pki, servicer)
    modbus_port = reserve_port()
    service_path, _grpc_port = _write_generator_tree(
        root,
        modbus_port=modbus_port,
        dg_endpoint=f"127.0.0.1:{dg_port}",
        scenario_id=SCENARIO_ID,
    )
    runtime = await _start_runtime(root, service_path)
    snapshots = ConfigSnapshots(runtime, service_path, root)
    gateway_grpc = create_gateway_grpc_server(runtime, snapshots)
    await gateway_grpc.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=modbus_port, timeout=5)
    await client.connect()
    try:
        yield runtime, servicer, client, gateway_grpc
    finally:
        client.close()
        await runtime.stop()
        await gateway_grpc.stop(grace=0)
        await server.stop(grace=0)


async def test_modbus_write_reaches_generator_over_mtls_and_is_accepted(linked_harness) -> None:
    runtime, servicer, client, _grpc = linked_harness
    result = await client.write_registers(ADDR[COMMAND_FLOAT], float_words(-40.25), device_id=1)
    assert not result.isError()
    record = await _wait_terminal(runtime, COMMAND_FLOAT, -40.25)
    assert record.state is ForwardState.ACCEPTED
    assert record.effective_step == 42
    assert record.device_id == DEVICE_ID
    assert len(servicer.update_log) == 1
    entry = servicer.update_log[0]
    assert entry["device_id"] == DEVICE_ID
    assert entry["generation_id"] == GENERATION_ID
    assert entry["request_id"] == record.event_id
    assert entry["values"] == [(COMMAND_FLOAT, -40.25)]


async def test_generator_rejection_is_terminal_rejected_and_not_retried(linked_harness) -> None:
    runtime, servicer, client, _grpc = linked_harness
    servicer.mode = "reject"
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(12.5), device_id=1)
    record = await _wait_terminal(runtime, COMMAND_FLOAT, 12.5)
    assert record.state is ForwardState.REJECTED
    # 生产语义：record.error 取 receipt.message（message 优先于 error_code）。
    assert record.error == "model rejected the command"
    await asyncio.sleep(0.5)
    # 尽力而为模型：拒绝后不得自动重试，服务端只收到一次提交。
    assert len(servicer.update_log) == 1
    # 同点后续新事件不被闭锁。
    servicer.mode = "accept"
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(13.5), device_id=1)
    follow_up = await _wait_terminal(runtime, COMMAND_FLOAT, 13.5)
    assert follow_up.state is ForwardState.ACCEPTED
    assert len(servicer.update_log) == 2


async def test_stale_generation_and_non_writable_point_are_definitive_rejections(
    linked_harness,
) -> None:
    runtime, servicer, client, _grpc = linked_harness
    servicer.mode = "stale"
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(1.0), device_id=1)
    stale = await _wait_terminal(runtime, COMMAND_FLOAT, 1.0)
    assert stale.state is ForwardState.REJECTED
    assert len(servicer.update_log) == 0  # 绑定校验失败，不得到达 UpdateVariables

    servicer.mode = "not_writable"
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(2.0), device_id=1)
    denied = await _wait_terminal(runtime, COMMAND_FLOAT, 2.0)
    assert denied.state is ForwardState.REJECTED
    assert len(servicer.update_log) == 0


async def test_accepted_without_effective_step_is_unknown_not_accepted(linked_harness) -> None:
    runtime, servicer, client, _grpc = linked_harness
    servicer.mode = "zero_step"
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(4.0), device_id=1)
    record = await _wait_terminal(runtime, COMMAND_FLOAT, 4.0)
    assert record.state is ForwardState.UNKNOWN
    assert record.effective_step == 0


async def test_two_writes_arrive_fifo_with_duplicate_request_ids(linked_harness) -> None:
    runtime, servicer, client, _grpc = linked_harness
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(21.0), device_id=1)
    first = await _wait_terminal(runtime, COMMAND_FLOAT, 21.0)
    await client.write_register(ADDR[COMMAND_UINT], 777, device_id=1)
    second = await _wait_terminal(runtime, COMMAND_UINT, 777)
    assert first.state is ForwardState.ACCEPTED and second.state is ForwardState.ACCEPTED
    # max_inflight=1：服务端观察到的请求顺序严格等于入队顺序，request_id 各自唯一。
    assert servicer.request_order == [first.event_id, second.event_id]
    assert len(set(servicer.request_order)) == 2
    assert servicer.update_log[0]["values"] == [(COMMAND_FLOAT, 21.0)]
    assert servicer.update_log[1]["values"] == [(COMMAND_UINT, 777)]


async def test_modbus_to_generator_update_latency_distribution(linked_harness) -> None:
    """S4 真实链路延迟基线：50 次顺序 Modbus 写 → mTLS DG 终态 RTT 分位数。"""
    runtime, servicer, client, _grpc = linked_harness
    samples = 50
    latencies_ms: list[float] = []
    for index in range(samples):
        value = 100.0 + index * 0.25  # 全部 float32 可精确表示，便于逐值匹配记录
        began = time.perf_counter()
        result = await client.write_registers(ADDR[COMMAND_FLOAT], float_words(value), device_id=1)
        assert not result.isError()
        record = await _wait_terminal(runtime, COMMAND_FLOAT, value)
        latencies_ms.append((time.perf_counter() - began) * 1e3)
        assert record.state is ForwardState.ACCEPTED
    ordered = sorted(latencies_ms)
    p99 = ordered[round(0.99 * (samples - 1))]
    print(
        "\n[S4 link latency ms]",
        {
            "samples": samples,
            "p50": round(statistics.median(ordered), 3),
            "p95": round(ordered[round(0.95 * (samples - 1))], 3),
            "p99": round(p99, 3),
            "max": round(ordered[-1], 3),
            "failures": 0,
        },
    )
    assert len(servicer.update_log) == samples
    # 配置契约 request_timeout_ms=5000：p99 必须留有充足裕度，不虚构更严 SLA。
    assert p99 < 5000.0


async def test_grpc_measurement_injection_never_reaches_generator(linked_harness) -> None:
    """场景 9：gRPC 测量注入与控制面隔离，绝不产生 data_generator 转发事件。"""
    runtime, servicer, client, gateway_grpc = linked_harness
    port = runtime.config.service.grpc.listen_port
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = irpc.IndustrialGatewayServiceStub(channel)
        claim = await stub.ClaimTargets(
            ipb.ClaimTargetsRequest(
                gateway_id=SERVER_ID,
                owner_id="s3-injection",
                variable_ids=[FEEDBACK_FLOAT],
                action="ACQUIRE",
                lease_ms=60_000,
            )
        )
        assert claim.accepted, claim.error_code
        reply = await stub.UpdateVariables(
            ipb.UpdateVariablesRequest(
                gateway_id=SERVER_ID,
                lease_token=claim.lease_token,
                values=[ipb.VariableUpdate(variable_id=FEEDBACK_FLOAT, value=encode_scalar(8.5))],
            )
        )
        assert all(result.success for result in reply.results)
    # 注入通道不得触发任何转发：DG 对端零提交，forwarder 无该点记录。
    await asyncio.sleep(0.3)
    assert servicer.update_log == []
    assert all(
        record.point_id != FEEDBACK_FLOAT for record in runtime.forwarders[SERVER_ID].records
    )
    # 控制转发器仍然存活：随后的 Modbus 控制写照常到达 DG。
    await client.write_registers(ADDR[COMMAND_FLOAT], float_words(5.5), device_id=1)
    record = await _wait_terminal(runtime, COMMAND_FLOAT, 5.5)
    assert record.state is ForwardState.ACCEPTED
    assert len(servicer.update_log) == 1


async def test_untrusted_client_certificate_ends_unknown_without_submission(
    tmp_path: Path,
) -> None:
    # 服务端 PKI 与 Gateway 客户端证书来自两个独立 CA：握手必失败。
    server_root = tmp_path / "server-side"
    client_root = tmp_path / "gateway-side"
    server_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    server_pki = write_mtls_pki(server_root, ("unused",))
    write_mtls_pki(client_root, ("gateway-dg",))
    servicer = FakeGeneratorServicer(scenario_id=SCENARIO_ID, writable_points=(COMMAND_FLOAT,))
    server, dg_port = await _start_generator_server(server_root, server_pki, servicer)
    # 拷贝客户端侧 PKI 进部署根：service_path 禁止解析到根外的符号链接路径。
    merged = client_root / "gw"
    merged.mkdir(parents=True)
    shutil.copytree(client_root / "certs", merged / "certs")
    modbus_port = reserve_port()
    service_path, _grpc_port = _write_generator_tree(
        merged,
        modbus_port=modbus_port,
        dg_endpoint=f"127.0.0.1:{dg_port}",
        scenario_id=SCENARIO_ID,
    )
    runtime = await _start_runtime(merged, service_path)
    client = AsyncModbusTcpClient("127.0.0.1", port=modbus_port, timeout=5)
    await client.connect()
    try:
        await client.write_registers(ADDR[COMMAND_FLOAT], float_words(9.0), device_id=1)
        record = await _wait_terminal(runtime, COMMAND_FLOAT, 9.0)
        assert record.state is ForwardState.UNKNOWN
        await asyncio.sleep(0.5)
        assert servicer.update_log == []  # 握手失败：零提交、零自动重试
        assert all(
            r.point_id != COMMAND_FLOAT or r.state != ForwardState.ACCEPTED
            for r in runtime.forwarders[SERVER_ID].records
        )
    finally:
        client.close()
        await runtime.stop()
        await server.stop(grace=0)
