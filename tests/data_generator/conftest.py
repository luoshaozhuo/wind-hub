"""Data Generator 验收测试共享 fixture。

铁律：成功路径一律走真实组件——真实 Core configs 树（site/devices/模板）、
真实 ``assemble_gateway`` 协议栈与正式 ``IndustrialGatewayService``（8 个 RPC，
含 ``UpdateVariables`` 与 ``ClaimTargets`` 租约围栏）、真实 gRPC Server/Client、
真实 CSV+SQLite。
故障注入仅用于无法用真实环境稳定构造的超时/挂起，且实现冻结的正式 Proto，
不得替代正式接口。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from data_generator.application.runtime import Runtime
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.gateway import GatewayWriter
from data_generator.infrastructure.server import make_server
from data_generator.infrastructure.snapshots import file_sha256, recover_transaction
from industrial_gateway.application.target_leases import TargetLeaseRegistry
from industrial_gateway.assembly import GatewayApplication, assemble_gateway
from industrial_gateway.infrastructure.config.loader import GatewayConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIGS = REPO_ROOT / "configs"
SRC_PATH = REPO_ROOT / "src"


def reserve_port() -> int:
    """预留一个 loopback TCP 端口（测试环境竞态窗口可接受）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


# --------------------------------------------------------------------------- #
# 真实 configs 树
# --------------------------------------------------------------------------- #


def _real_tree(root: Path) -> None:
    """从仓库 configs/ 拷贝真实 Core 共享文件（唯一允许的模板来源）。"""
    for name in ("instances", "templates", "history"):
        source = REPO_CONFIGS / name
        target = root / name
        for item in source.rglob("*"):
            if item.is_file():
                destination = target / item.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.read_bytes())


def write_service_yaml(
    root: Path,
    *,
    bind: str,
    output_timeout: float = 2.0,
    max_inflight: int = 16,
    diagnose_concurrent: int = 4,
    diagnose_wait: float | None = None,
    metrics_max_series: int = 2000,
) -> Path:
    """生成服务配置（直接路径契约：相对 configs 根，禁止绝对路径与 ..）。"""
    service = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "configuration": {
            "instances": {
                "site": "instances/site.yaml",
                "devices": "instances/devices.yaml",
                "scenarios": "instances/data-generator.yaml",
            },
            "templates": {"root": "templates"},
        },
        "service": {"grpc_bind": bind},
        "output": {"max_inflight": max_inflight, "grpc_timeout_seconds": output_timeout},
        "buffer": {"max_batches": 128, "max_points_per_batch": 1000},
        "replay": {"read_batch_size": 1000},
        "diagnose": {
            "max_concurrent": diagnose_concurrent,
            "timeout_seconds": 10.0,
            **({"permit_wait_seconds": diagnose_wait} if diagnose_wait is not None else {}),
        },
        "metrics": {
            "sample_seconds": 1.0,
            "window_seconds": 60,
            "retain_seconds": 3600,
            "max_series": metrics_max_series,
        },
    }
    path = root / "services" / "data-generator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(service, sort_keys=False), encoding="utf-8")
    return path


def write_scenarios(root: Path, document: dict[str, Any]) -> Path:
    path = root / "instances" / "data-generator.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def write_history(root: Path, name: str, rows: Sequence[str], *, header: bool = True) -> Path:
    path = root / "history" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header:
        lines.append("device_id,point_id,source_timestamp,value,quality,record_id")
    lines.extend(rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def configs_root(tmp_path: Path) -> Callable[..., Path]:
    """在 tmp_path 下构造受控 configs 根；默认整树拷贝仓库真实 Core 配置。"""

    created: list[Path] = []

    def factory(
        *,
        scenarios: dict[str, Any] | None = None,
        bind: str | None = None,
        output_timeout: float = 2.0,
        max_inflight: int = 16,
        diagnose_concurrent: int = 4,
        diagnose_wait: float | None = None,
        metrics_max_series: int = 2000,
        history: dict[str, Sequence[str]] | None = None,
    ) -> Path:
        root = tmp_path / f"configs-{len(created)}"
        created.append(root)
        _real_tree(root)
        if history:
            for name, rows in history.items():
                write_history(root, name, rows)
        if scenarios is not None:
            write_scenarios(root, scenarios)
        service_path = write_service_yaml(
            root,
            bind=bind or f"127.0.0.1:{reserve_port()}",
            output_timeout=output_timeout,
            max_inflight=max_inflight,
            diagnose_concurrent=diagnose_concurrent,
            diagnose_wait=diagnose_wait,
            metrics_max_series=metrics_max_series,
        )
        return service_path

    return factory


def simulation_document(
    gateways: dict[str, str],
    specs: Sequence[tuple[str, str, dict[str, Any] | None, Sequence[tuple[str, str]]]],
    *,
    scenario_id: str = "demo-random",
    version: str = "1.0.0",
    interval: float = 1.0,
    seed: int = 42,
) -> dict[str, Any]:
    """通用模拟场景构造器。

    specs: [(device_id, point_id, model(None=random 3..15), [(gateway_id, variable_id)])]
    """
    devices: dict[str, list[dict[str, Any]]] = {}
    per_gateway: dict[str, list[dict[str, str]]] = {gateway_id: [] for gateway_id in gateways}
    for device_id, point_id, model, mappings in specs:
        devices.setdefault(device_id, []).append(
            {
                "point_id": point_id,
                "model": model
                or {
                    "kind": "random",
                    "distribution": "uniform",
                    "minimum": 3.0,
                    "maximum": 15.0,
                },
            }
        )
        for gateway_id, variable_id in mappings:
            per_gateway[gateway_id].append(
                {"device_id": device_id, "point_id": point_id, "variable_id": variable_id}
            )
    gateway_entries = [
        {
            "gateway_id": gateway_id,
            "endpoint": endpoint,
            "feedback_mapping": per_gateway[gateway_id],
        }
        for gateway_id, endpoint in sorted(gateways.items())
    ]
    return {
        "schema_version": 1,
        "file_version": "1.0.0",
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "version": version,
                "description": "acceptance generated scenario",
                "source": {"kind": "simulation", "interval_seconds": interval, "seed": seed},
                "devices": [
                    {"device_id": device_id, "points": points}
                    for device_id, points in sorted(devices.items())
                ],
                "gateways": gateway_entries,
            }
        ],
    }


def ship_scenarios_document(
    gateways: dict[str, str],
    *,
    scenario_id: str = "demo-random",
    interval: float = 1.0,
    seed: int = 42,
    minimum: float = 3.0,
    maximum: float = 15.0,
    device: str = "GW01",
    point: str = "a02_dm_pitchangle1",
    mappings: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """基于真实 Core 点表点 a02_dm_pitchangle1 构造单点随机场景。"""
    specs = [
        (
            device,
            point,
            {
                "kind": "random",
                "distribution": "uniform",
                "minimum": minimum,
                "maximum": maximum,
            },
            mappings or [],
        )
    ]
    return simulation_document(
        gateways,
        specs,
        scenario_id=scenario_id,
        interval=interval,
        seed=seed,
    )


def replay_document(
    gateways: dict[str, str],
    *,
    file: str = "history/short.csv",
    scenario_id: str = "demo-replay",
    speed: float = 100.0,
    start: str = "2026-01-01T00:00:00Z",
    end: str = "2026-01-01T01:00:00Z",
    device: str = "GW02",
    point: str = "a02_dm_pitchangle1",
    mappings: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    if mappings is None:
        mappings = [("gw-test", f"{device}.pitch_angle")]
    return {
        "schema_version": 1,
        "file_version": "1.0.0",
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "version": "1.0.0",
                "description": "acceptance replay scenario",
                "source": {
                    "kind": "replay",
                    "history_reader": "csv",
                    "file": file,
                    "start_time": start,
                    "end_time": end,
                    "speed": speed,
                    "loop": False,
                },
                "devices": [{"device_id": device, "points": [{"point_id": point}]}],
                "gateways": [
                    {
                        "gateway_id": gateway_id,
                        "endpoint": endpoint,
                        "feedback_mapping": [
                            {"device_id": device, "point_id": point, "variable_id": variable_id}
                            for mapped_gw, variable_id in mappings
                            if mapped_gw == gateway_id
                        ],
                    }
                    for gateway_id, endpoint in sorted(gateways.items())
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# 正式 Proto 故障注入网关（实现冻结的 IndustrialGatewayService 八 RPC 契约；
# 仅用于超时/挂起/拒绝等真实 Gateway 无法稳定构造的故障，成功路径禁止使用）
# --------------------------------------------------------------------------- #


class FaultGatewayService(gw_rpc.IndustrialGatewayServiceServicer):
    """按 gateway_id 注入行为的正式契约服务；UpdateVariables 是唯一数据入口。"""

    # 跨实例（多个故障网关 Server）聚合的在途并发峰值，用于验证全局许可有界。
    global_active = 0
    global_max_active = 0

    @classmethod
    def reset_concurrency(cls) -> None:
        cls.global_active = 0
        cls.global_max_active = 0

    def __init__(
        self,
        catalog: dict[str, Sequence[tuple[str, str, bool]]],
        behaviors: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        # catalog: gateway_id -> [(variable_id, data_type, writable), ...]
        self._catalog = {gateway_id: list(items) for gateway_id, items in catalog.items()}
        self._behaviors = behaviors or {}
        self.update_calls: dict[str, list[list[str]]] = {gateway_id: [] for gateway_id in catalog}
        self.list_calls: dict[str, int] = {gateway_id: 0 for gateway_id in catalog}
        # 复用生产租约注册表：故障服务的所有权判定与正式 IndustrialGatewayService 同源。
        self.leases = TargetLeaseRegistry()
        self.claim_calls: dict[str, list[dict[str, Any]]] = {
            gateway_id: [] for gateway_id in catalog
        }
        self.store: dict[str, dict[str, Any]] = {gateway_id: {} for gateway_id in catalog}

    def _variables(self, gateway_id: str, flt: str) -> list[gw.Variable]:
        return [
            gw.Variable(variable_id=name, name=name, data_type=dtype, writable=writable)
            for name, dtype, writable in self._catalog.get(gateway_id, ())
            if flt in name
        ]

    async def GetGateway(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")

    async def StartGateway(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")

    async def StopGateway(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")

    async def ListGateways(self, request, context):  # noqa: ANN001, ANN202, N802
        return gw.ListGatewaysResponse(
            gateways=[
                gw.Gateway(gateway_id=gateway_id, actual_state="RUNNING")
                for gateway_id in self._catalog
            ]
        )

    async def ListVariables(self, request, context):  # noqa: ANN001, ANN202, N802
        behavior = self._behaviors.get(request.gateway_id, {})
        if "list_abort" in behavior:
            await context.abort(behavior["list_abort"], "injected list failure")
        gate = behavior.get("list_gate")
        if gate is not None:
            await gate.wait()
        delay = behavior.get("list_delay", 0.0)
        if delay:
            await asyncio.sleep(delay)
        self.list_calls[request.gateway_id] = self.list_calls.get(request.gateway_id, 0) + 1
        return gw.ListVariablesResponse(
            variables=self._variables(request.gateway_id, request.filter)
        )

    async def ClaimTargets(self, request, context):  # noqa: ANN001, ANN202, N802
        """与正式 data_server.ClaimTargets 同语义：复用生产 TargetLeaseRegistry。"""
        behavior = self._behaviors.get(request.gateway_id, {})
        ids = list(request.variable_ids)
        self.claim_calls.setdefault(request.gateway_id, []).append(
            {
                "action": request.action,
                "owner_id": request.owner_id,
                "token": request.lease_token,
                "variables": ids,
                "lease_ms": request.lease_ms,
            }
        )
        abort_by_action = behavior.get("claim_abort", {})
        abort_status = abort_by_action.get(request.action) or behavior.get("claim_abort_all")
        gate = behavior.get("claim_gate")
        if gate is not None:
            gate_map = behavior.get("claim_gate_actions")
            if gate_map is None or request.action in gate_map:
                await gate.wait()
        delay = behavior.get("claim_delay", 0.0)
        if delay:
            await asyncio.sleep(delay)
        if abort_status is not None:
            await context.abort(abort_status, "injected claim failure")
        catalog_ids = {
            name for name, _dtype, writable in self._catalog.get(request.gateway_id, ()) if writable
        }
        if not ids or len(set(ids)) != len(ids) or any(name not in catalog_ids for name in ids):
            return gw.ClaimTargetsResponse(accepted=False, error_code="INVALID_TARGETS")
        async with self.leases.lock:
            accepted, token, error = self.leases.claim(
                request.gateway_id,
                request.owner_id,
                set(ids),
                request.action,
                request.lease_token,
                request.lease_ms,
            )
        self.claim_calls[request.gateway_id][-1]["response_token"] = token
        return gw.ClaimTargetsResponse(accepted=accepted, lease_token=token, error_code=error)

    async def UpdateVariables(self, request, context):  # noqa: ANN001, ANN202, N802
        behavior = self._behaviors.get(request.gateway_id, {})
        ids = [item.variable_id for item in request.values]
        # 服务端强制租约围栏：无令牌/过期/他主令牌在写入前一律 FAILED_PRECONDITION。
        async with self.leases.lock:
            if not self.leases.authorize(request.gateway_id, set(ids), request.lease_token):
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "LEASE_LOST")
        self.update_calls.setdefault(request.gateway_id, []).append(ids)
        FaultGatewayService.global_active += 1
        FaultGatewayService.global_max_active = max(
            FaultGatewayService.global_max_active, FaultGatewayService.global_active
        )
        try:
            if "abort" in behavior:
                await context.abort(behavior["abort"], "injected update failure")
            gate = behavior.get("gate")
            if gate is not None:
                await gate.wait()
            delay = behavior.get("delay", 0.0)
            if delay:
                await asyncio.sleep(delay)
            per_item_fail = behavior.get("per_item_fail", frozenset())
            omit = behavior.get("omit", frozenset())
            results = []
            for item in request.values:
                if item.variable_id in omit:
                    continue
                success = item.variable_id not in per_item_fail
                if success:
                    self.store[request.gateway_id][item.variable_id] = item.value
                results.append(
                    gw.VariableUpdateResult(
                        variable_id=item.variable_id,
                        success=success,
                        error_code="INJECTED" if not success else "",
                    )
                )
            return gw.UpdateVariablesResponse(results=results)
        finally:
            FaultGatewayService.global_active -= 1

    async def ReadVariables(self, request, context):  # noqa: ANN001, ANN202, N802
        values = []
        for variable_id in request.variable_ids:
            raw = self.store.get(request.gateway_id, {}).get(variable_id)
            if raw is None:
                sample = common.Sample(
                    point_id=variable_id, quality="UNKNOWN", error_code="NO_VALUE"
                )
            else:
                sample = common.Sample(point_id=variable_id, quality="GOOD", value=raw)
            values.append(sample)
        return gw.ReadVariablesResponse(gateway_id=request.gateway_id, values=values)


@dataclass(slots=True)
class FaultGateway:
    server: grpc.aio.Server
    service: FaultGatewayService
    bind: str

    async def stop(self) -> None:
        await self.server.stop(grace=0)


@pytest.fixture
async def start_fault_gateway() -> AsyncIterator[Callable[..., Awaitable[FaultGateway]]]:
    servers: list[FaultGateway] = []

    async def factory(
        catalog: dict[str, Sequence[tuple[str, str, bool]]],
        behaviors: dict[str, dict[str, Any]] | None = None,
    ) -> FaultGateway:
        server = grpc.aio.server()
        service = FaultGatewayService(catalog, behaviors)
        gw_rpc.add_IndustrialGatewayServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        fault = FaultGateway(server, service, f"127.0.0.1:{port}")
        servers.append(fault)
        return fault

    yield factory
    for server in reversed(servers):
        await server.stop()


# --------------------------------------------------------------------------- #
# 真实 Industrial Gateway
# --------------------------------------------------------------------------- #


def build_data_gateway_config(
    *,
    endpoint_id: str,
    data_bind: str,
    devices: dict[str, Sequence[tuple[str, str, str]]],
    data_writers: Sequence[str] = ("127.0.0.1",),
    journal_path: Path | None = None,
    control_enabled: bool = False,
    commander_endpoint: str = "127.0.0.1:9",
) -> GatewayConfig:
    """仅含 feedback 虚拟点的真实 Gateway 配置；变量名=设备.点。

    devices: 逻辑设备名 -> [(point_id, data_type, role), ...]
    """
    modbus_port = reserve_port()
    unique_points = sorted({point for points in devices.values() for point in points})
    points_table: dict[str, dict[str, Any]] = {}
    holding_cursor = 100
    coil_cursor = 0
    for point_id, data_type, role in unique_points:
        if data_type == "bool":
            address = {"register_type": "coil", "address": coil_cursor}
            coil_cursor += 1
        else:
            address = {"register_type": "holding", "address": holding_cursor}
            holding_cursor += 2 if data_type == "float32" else 1
        points_table[point_id] = {
            "role": role,
            "data_type": data_type,
            "address": address,
            **({"max_age_seconds": 30.0} if role == "feedback" else {}),
        }
    table = {"protocol": "modbus", "points": points_table}
    config: dict[str, Any] = {
        "data_grpc_bind": data_bind,
        "endpoints": {
            endpoint_id: {
                "protocol": "modbus",
                "host": "127.0.0.1",
                "port": modbus_port,
                "protocol_options": {"mode": "tcp"},
            }
        },
        "point_tables": {"dg_points": table},
        "devices": {
            device_id: {
                "endpoint_id": endpoint_id,
                "point_table_id": "dg_points",
                "protocol_address": {"unit_id": 1},
                "commands": {
                    point_id: {"device_id": f"phys-{device_id}", "point_id": point_id}
                    for point_id, _data_type, role in points
                    if role == "command"
                },
            }
            for device_id, points in devices.items()
        },
        "control": {
            "enabled": control_enabled,
            "commander": {"endpoint": commander_endpoint},
            "journal_path": str(journal_path or Path("/tmp/dg-acceptance-journal.json")),
            "access": {
                "data_writers": list(data_writers),
                "command_writers": [],
            },
        },
    }
    return GatewayConfig.model_validate(config)


@dataclass(slots=True)
class RealGateway:
    application: GatewayApplication
    endpoint_id: str
    data_bind: str

    async def stop(self) -> None:
        await self.application.stop()


@pytest.fixture
async def start_real_gateway() -> AsyncIterator[Callable[..., Awaitable[RealGateway]]]:
    running: list[RealGateway] = []

    async def factory(
        *,
        endpoint_id: str,
        devices: dict[str, Sequence[tuple[str, str, str]]] | None = None,
        data_bind: str | None = None,
        data_writers: Sequence[str] = ("127.0.0.1",),
        journal_path: Path | None = None,
    ) -> RealGateway:
        bind = data_bind or f"127.0.0.1:{reserve_port()}"
        if devices is None:
            devices = {"GW01": [("pitch_angle", "float32", "feedback")]}
        config = build_data_gateway_config(
            endpoint_id=endpoint_id,
            data_bind=bind,
            devices=devices,
            data_writers=data_writers,
            journal_path=journal_path,
        )
        application = assemble_gateway(config)
        await application.start()
        gateway = RealGateway(application, endpoint_id, bind)
        running.append(gateway)
        return gateway

    yield factory
    for gateway in reversed(running):
        await gateway.stop()


# --------------------------------------------------------------------------- #
# 真实 Data Generator 进程内 Server
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DataGeneratorHarness:
    runtime: Runtime
    server: grpc.aio.Server
    bind: str
    service_path: Path
    dg: Any  # DataGeneratorService stub
    common: Any  # CommonService stub
    channel: grpc.aio.Channel

    async def stop(self) -> None:
        await self.server.stop(grace=1.0)
        await self.channel.close()
        await self.runtime.shutdown()


@pytest.fixture
async def dg_server(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[DataGeneratorHarness]]]:
    harnesses: list[DataGeneratorHarness] = []

    async def factory(service_path: Path, *, timeout: float | None = None) -> DataGeneratorHarness:
        recover_transaction(service_path.absolute().parent.parent)
        settings = load_settings(service_path)
        output = settings.service.get("output", {})
        writer = GatewayWriter(
            timeout=float(output.get("grpc_timeout_seconds", 3.0)) if timeout is None else timeout,
            max_points=int(settings.service.get("buffer", {}).get("max_points_per_batch", 1000)),
            max_inflight=int(output.get("max_inflight", 1)),
        )
        runtime = Runtime(settings, writer)
        bind = settings.service.get("service", {}).get("grpc_bind", f"127.0.0.1:{reserve_port()}")
        server = make_server(runtime, bind)
        await server.start()
        channel = grpc.aio.insecure_channel(bind)
        await channel.channel_ready()
        harness = DataGeneratorHarness(
            runtime,
            server,
            bind,
            service_path,
            dg_rpc.DataGeneratorServiceStub(channel),
            common_rpc.CommonServiceStub(channel),
            channel,
        )
        harnesses.append(harness)
        return harness

    yield factory
    for harness in reversed(harnesses):
        await harness.stop()


# --------------------------------------------------------------------------- #
# 独立 Data Generator 子进程
# --------------------------------------------------------------------------- #


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


async def wait_for_port(bind: str, timeout: float = 15.0) -> None:
    host, port_text = bind.rsplit(":", 1)
    port = int(port_text)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(_port_open, host, port):
            await asyncio.sleep(0.15)
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"service did not bind {bind}")


@pytest.fixture
def dg_subprocess(tmp_path: Path):
    processes: list[subprocess.Popen[bytes]] = []

    async def factory(
        service_path: Path, *, wait_ready: bool = True
    ) -> tuple[str, subprocess.Popen[bytes]]:
        bind_text = yaml.safe_load(service_path.read_text(encoding="utf-8"))["service"]["grpc_bind"]
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(SRC_PATH) + (os.pathsep + existing if existing else "")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "data_generator.main",
                "--config",
                str(service_path),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
        if wait_ready:
            await wait_for_port(bind_text)
        return bind_text, process

    yield factory

    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@asynccontextmanager
async def grpc_channel(bind: str) -> AsyncIterator[grpc.aio.Channel]:
    channel = grpc.aio.insecure_channel(bind)
    await channel.channel_ready()
    try:
        yield channel
    finally:
        await channel.close()


async def read_gateway_variable(bind: str, gateway_id: str, variable_id: str) -> common.Sample:
    """通过正式 ReadVariables 读回单个变量。"""
    async with grpc.aio.insecure_channel(bind) as channel:
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        response = await stub.ReadVariables(
            gw.ReadVariablesRequest(gateway_id=gateway_id, variable_ids=[variable_id])
        )
    sample: common.Sample = response.values[0]
    return sample


def metric_map(metrics_response: Any) -> dict[str, float]:
    return {item.name: item.value for item in metrics_response.metrics}


async def dg_stubs(bind: str) -> tuple[grpc.aio.Channel, Any, Any]:
    """独立服务进程的真实 gRPC 客户端（DG 6 + Common 9 同一信道）。"""
    channel = grpc.aio.insecure_channel(bind)
    await channel.channel_ready()
    return channel, dg_rpc.DataGeneratorServiceStub(channel), common_rpc.CommonServiceStub(channel)


def replace_snapshot_files(
    snapshot: Any, replacements: dict[str, bytes]
) -> list[common.SnapshotFile]:
    """在完整文件集快照上替换私有文件内容，用于 Push 候选构造。"""
    files = []
    for item in snapshot.files:
        data = replacements.get(item.relative_path, item.content)
        files.append(
            common.SnapshotFile(
                relative_path=item.relative_path,
                content=data,
                size_bytes=len(data),
                sha256=file_sha256(data),
            )
        )
    return files
