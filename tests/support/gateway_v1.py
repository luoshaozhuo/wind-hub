"""Industrial Gateway v1 真实链路测试支撑（无测试期垫片）。

- 在临时部署根内写出完整 v1 配置树（Service + devices 实例 + 版本化模板），
  所有解析走生产 ``load_gateway`` 路径；
- 真实 ``GatewayRuntime`` + 真实 gRPC aio 服务端（Gateway 10 RPC + CommonService；
  其中两个仿真执行 RPC 业务未实现，由生成 Servicer 基类返回 UNIMPLEMENTED）；
- 真实 gRPC aio Commander 对端（GetDevice/SubmitCommand/GetCommandResult），
  逐 (device, point) 配置 accepted/rejected、延迟与在途并发观测；
- 真实 pymodbus 3.15 ``AsyncModbusTcpClient`` 访问每设备独立 Modbus Server。
"""

from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import grpc
import yaml
from pymodbus.client import AsyncModbusTcpClient

from contracts import commander_pb2 as cpb
from contracts import commander_pb2_grpc as crpc
from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as ipb
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.config.resolution import load_gateway
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots
from industrial_gateway.infrastructure.grpc.data_server import (
    create_gateway_grpc_server,
)
from industrial_gateway.infrastructure.runtime import GatewayRuntime

# 点表：所有测试设备共享同一份版本化模板。
FEEDBACK_FLOAT = "fb_float"
FEEDBACK_PAIR = "fb_pair"
FEEDBACK_UINT = "fb_uint"
FEEDBACK_COIL_0 = "fb_coil_0"
FEEDBACK_COIL_1 = "fb_coil_1"
FEEDBACK_COIL_15 = "fb_coil_15"
FEEDBACK_COIL_16 = "fb_coil_16"
FEEDBACK_COIL_17 = "fb_coil_17"
FEEDBACK_DISCRETE = "fb_discrete"
FEEDBACK_INPUT = "fb_input"
COMMAND_FLOAT = "cmd_float"
COMMAND_UINT2 = "cmd_uint2"
COMMAND_UINT = "cmd_uint"
COMMAND_COIL = "cmd_coil"
COMMAND_COIL_B = "cmd_coil_b"

POINT_TABLE: dict[str, dict[str, Any]] = {
    FEEDBACK_FLOAT: {
        "data_type": "float32",
        "address": {
            "register_type": "holding",
            "address": 100,
            "word_order": "big_endian",
        },
    },
    FEEDBACK_PAIR: {
        "data_type": "uint16",
        "address": {"register_type": "holding", "address": 102},
    },
    FEEDBACK_UINT: {
        "data_type": "uint16",
        "address": {"register_type": "holding", "address": 110},
    },
    FEEDBACK_COIL_0: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 0},
    },
    FEEDBACK_COIL_1: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 1},
    },
    FEEDBACK_COIL_15: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 15},
    },
    FEEDBACK_COIL_16: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 16},
    },
    FEEDBACK_COIL_17: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 17},
    },
    FEEDBACK_DISCRETE: {
        "data_type": "bool",
        "address": {"register_type": "discrete_input", "address": 0},
    },
    FEEDBACK_INPUT: {
        "data_type": "int16",
        "address": {"register_type": "input", "address": 365},
    },
    COMMAND_FLOAT: {
        "data_type": "float32",
        "address": {
            "register_type": "holding",
            "address": 200,
            "word_order": "big_endian",
        },
    },
    COMMAND_UINT2: {
        "data_type": "uint16",
        "address": {"register_type": "holding", "address": 202},
    },
    COMMAND_UINT: {
        "data_type": "uint16",
        "address": {"register_type": "holding", "address": 210},
    },
    COMMAND_COIL: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 32},
    },
    COMMAND_COIL_B: {
        "data_type": "bool",
        "address": {"register_type": "coil", "address": 33},
    },
}

# 设计约束：commander_point_id 必须是绑定设备点表中的测点；v1 控制点映射到
# Commander 同名点（不同命名在专项测试中单独构造）。
DEFAULT_COMMAND_MAPPINGS: tuple[tuple[str, str], ...] = (
    (COMMAND_FLOAT, COMMAND_FLOAT),
    (COMMAND_UINT2, COMMAND_UINT2),
    (COMMAND_UINT, COMMAND_UINT),
    (COMMAND_COIL, COMMAND_COIL),
    (COMMAND_COIL_B, COMMAND_COIL_B),
)


# 进程内已分发端口集合：连续 bind(0)/close 在高并发分配时会复用同一端口，
# 导致一台子进程拿到两个相同监听端口而被配置校验拒绝（实测竞态）。
_RESERVED_PORTS: set[int] = set()


def reserve_port() -> int:
    """预留一个本进程内唯一的本机 TCP 端口（关闭套接字后交调用方绑定）。"""
    for _ in range(100):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        finally:
            sock.close()
        if port not in _RESERVED_PORTS:
            _RESERVED_PORTS.add(port)
            return port
    raise RuntimeError("could not allocate a unique test port")


def float_words(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def words_float(words: Sequence[int]) -> float:
    return float(struct.unpack(">f", struct.pack(">HH", *words))[0])


@dataclass(frozen=True, slots=True)
class ServerSpec:
    server_id: str
    device_id: str
    modbus_port: int
    slave_id: int = 1
    desired_state: str = "START"
    control_enabled: bool = True
    command_mappings: tuple[tuple[str, str], ...] = DEFAULT_COMMAND_MAPPINGS


@dataclass(slots=True)
class CommanderCall:
    device_id: str
    point_id: str
    command_id: str
    value: bool | int | float | str | bytes
    revision: str


@dataclass(slots=True)
class _Gate:
    inflight: dict[str, int] = field(default_factory=dict)
    max_inflight: dict[str, int] = field(default_factory=dict)


class FakeCommanderService(crpc.CommanderServiceServicer):
    """真实 grpc.aio Commander：按 (device, point) 配置裁决、延迟与并发观测。"""

    def __init__(self) -> None:
        self.calls: list[CommanderCall] = []
        self.accepted: dict[tuple[str, str], bool] = {}
        self.messages: dict[tuple[str, str], str] = {}
        self.delays: dict[tuple[str, str], float] = {}
        self.revisions: dict[str, str] = {}
        self.results: dict[str, cpb.GetCommandResultResponse] = {}
        self.rpc_aborts: dict[tuple[str, str], grpc.StatusCode] = {}
        self.get_device_calls: list[str] = []
        self.get_device_hook: Any = None
        # 真实入站 SubmitCommand 次数（abort 在记录 calls 之前发生，
        # 该计数用于证明 abort 场景恰好送达一次且无重试）。
        self.submit_attempts: dict[tuple[str, str], int] = {}
        # accepted=True 但故意不返回 command_id 的目标（权威空 ID → UNKNOWN）。
        self.accepted_without_id: set[tuple[str, str]] = set()
        self._gate = _Gate()

    def set_rejected(self, device: str, point: str, *, message: str = "rejected") -> None:
        self.accepted[(device, point)] = False
        self.messages[(device, point)] = message

    def set_delay(self, device: str, point: str, seconds: float) -> None:
        self.delays[(device, point)] = seconds

    def revision(self, device: str) -> str:
        return self.revisions.get(device, f"rev-{device}")

    def max_inflight(self, device: str, point: str) -> int:
        return self._gate.max_inflight.get(f"{device}.{point}", 0)

    async def get_device(
        self, request: common.ObjectId, context: grpc.aio.ServicerContext
    ) -> cpb.GetDeviceResponse:
        self.get_device_calls.append(request.id)
        if self.get_device_hook is not None:
            return await self.get_device_hook(request.id, context)
        return cpb.GetDeviceResponse(
            device=cpb.Device(
                device_id=request.id,
                protocol="modbus",
                endpoint="127.0.0.1:0",
                revision=self.revision(request.id),
                availability="AVAILABLE",
            )
        )

    async def submit_command(
        self, request: cpb.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> cpb.SubmitCommandResponse:
        kind = request.value.WhichOneof("kind")
        assert kind is not None
        raw: bool | int | float | str | bytes = getattr(request.value, kind)
        key = (request.device_id, request.point_id)
        gate_key = f"{request.device_id}.{request.point_id}"
        self.submit_attempts[key] = self.submit_attempts.get(key, 0) + 1
        current = self._gate.inflight.get(gate_key, 0) + 1
        self._gate.inflight[gate_key] = current
        self._gate.max_inflight[gate_key] = max(current, self._gate.max_inflight.get(gate_key, 0))
        abort = self.rpc_aborts.get(key)
        if abort is not None:
            await context.abort(abort, "injected commander failure")
        command_id = f"cmd-{len(self.calls) + 1:04d}-{request.device_id}-{request.point_id}"
        self.calls.append(
            CommanderCall(
                device_id=request.device_id,
                point_id=request.point_id,
                command_id=command_id,
                value=raw,
                revision=request.expected_revision,
            )
        )
        try:
            delay = self.delays.get(key, 0.0)
            if delay:
                await asyncio.sleep(delay)
            is_accepted = self.accepted.get(key, True)
            message = self.messages.get(key, "")
            state = "ACCEPTED" if is_accepted else "REJECTED"
            omit_id = is_accepted and key in self.accepted_without_id
            return cpb.SubmitCommandResponse(
                command_id="" if omit_id else (command_id if is_accepted else ""),
                accepted=is_accepted,
                state=state,
                outcome="",
                error_code="" if is_accepted else "REJECTED",
                message=message,
            )
        finally:
            self._gate.inflight[gate_key] -= 1

    async def get_command_result(
        self, request: cpb.GetCommandResultRequest, context: object
    ) -> cpb.GetCommandResultResponse:
        return self.results.get(
            request.command_id,
            cpb.GetCommandResultResponse(
                command_id=request.command_id,
                state="UNKNOWN",
                outcome="",
                message="no authoritative result",
            ),
        )

    # gRPC 处理器按 proto 大写名注册（snake_case 定义避免 N802 豁免）。
    GetDevice = get_device
    SubmitCommand = submit_command
    GetCommandResult = get_command_result


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def write_mtls_pki(root: Path, client_names: Sequence[str]) -> dict[str, object]:
    """生成真实自签 PKI（EC P-256）：一个 CA、服务器证书、按 CN 的客户端证书。"""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    certs_dir = root / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def leaf(name: str, *, client: bool) -> tuple[Path, Path]:
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH
                        if client
                        else x509.oid.ExtendedKeyUsageOID.SERVER_AUTH
                    ]
                ),
                critical=True,
            )
        )
        if not client:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
        cert = builder.sign(ca_key, hashes.SHA256())
        prefix = "client-" + name if client else "server"
        key_path = certs_dir / f"{prefix}.key"
        cert_path = certs_dir / f"{prefix}.crt"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path

    ca_path = certs_dir / "ca.crt"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_crt, server_key = leaf("gateway-server", client=False)
    clients = {name: leaf(name, client=True) for name in client_names}
    return {
        "ca": ca_path,
        "server_crt": server_crt,
        "server_key": server_key,
        "clients": clients,
    }


def write_config_tree(
    root: Path,
    specs: Sequence[ServerSpec],
    *,
    commander_endpoint: str,
    grpc_port: int,
    service_name: str = "service.yaml",
    forward_queue_capacity: int = 1024,
    max_forward_inflight: int = 16,
    submit_deadline_ms: int = 5000,
    metrics_window_seconds: int = 60,
    permissions: dict[str, Sequence[str]] | None = None,
    mtls_enabled: bool = False,
    point_table: dict[str, dict[str, Any]] | None = None,
    simulation: dict[str, Any] | None = None,
) -> Path:
    """写出一份完整、自洽的 v1 配置树，返回 Service 文件路径。"""
    devices: dict[str, dict[str, Any]] = {}
    servers: list[dict[str, Any]] = []
    for spec in specs:
        devices[spec.device_id] = {
            "name": f"模拟设备 {spec.device_id}",
            "enabled": True,
            "model": {"id": "gw_turbine", "version": "1.0.0"},
            "endpoint": {"host": "127.0.0.1", "port": spec.modbus_port},
            "overrides": {"protocol_options": {"slave_id": spec.slave_id}},
        }
        if spec.control_enabled:
            control: dict[str, Any] = {
                "enabled": True,
                "target": {
                    "kind": "commander",
                    "endpoint": commander_endpoint,
                    "queue_capacity": forward_queue_capacity,
                    "max_inflight": max_forward_inflight,
                    "request_timeout_ms": submit_deadline_ms,
                },
                "mappings": [
                    {"variable_id": variable_id, "target_point_id": target_point_id}
                    for variable_id, target_point_id in spec.command_mappings
                ],
                # Commander 授权后的专用 Generator 仿真绑定（直连 data_generator 已删除）。
                **({"simulation": simulation} if simulation is not None else {}),
            }
        else:
            control = {
                "enabled": False,
                "target": {"kind": "none"},
                "mappings": [],
            }
        server: dict[str, Any] = {
            "server_id": spec.server_id,
            "device_id": spec.device_id,
            "listen_host": "127.0.0.1",
            "listen_port": spec.modbus_port,
            "desired_state": spec.desired_state,
            "control": control,
        }
        servers.append(server)

    _dump(
        root / "instances" / "devices.yaml",
        {"schema_version": 1, "file_version": "1.0.0", "devices": devices},
    )
    _dump(
        root / "instances" / "site.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "site_id": "gateway-test-site",
            "name": "Gateway v1 测试现场",
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
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "business_points": {},
        },
    )
    _dump(
        root / "templates" / "protocols" / "protocol_gw_modbus_v1.0.0.yaml",
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
        root / "templates" / "device_models" / "device_model_gw_turbine_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "gw_turbine",
            "file_version": "1.0.0",
            "name": "Gateway v1 测试机型",
            "device_type_id": "turbine",
            "protocol": {"id": "gw_modbus", "version": "1.0.0"},
            "point_table": {"id": "gw_points", "version": "1.0.0"},
        },
    )
    _dump(
        root / "templates" / "point_tables" / "point_table_gw_points_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "gw_points",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "points": {key: dict(value) for key, value in (point_table or POINT_TABLE).items()},
        },
    )
    service: dict[str, Any] = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "config_sources": {
            "instances": {"devices": "instances/devices.yaml"},
            "templates": {"root": "templates"},
        },
        "grpc": {
            "listen_host": "127.0.0.1",
            "listen_port": grpc_port,
            "max_active_rpcs": 64,
            "request_max_bytes": 2097152,
            "tls": (
                {
                    "enabled": True,
                    "cert_file": "certs/server.crt",
                    "key_file": "certs/server.key",
                    "ca_file": "certs/ca.crt",
                }
                if mtls_enabled
                else {"enabled": False}
            ),
            **(
                {"permissions": {name: list(rights) for name, rights in permissions.items()}}
                if permissions
                else {}
            ),
        },
        "servers": servers,
        "metrics": {
            "window_seconds": metrics_window_seconds,
            "collect_interval_ms": 1000,
        },
    }
    service_path = root / service_name
    _dump(service_path, service)
    return service_path


@dataclass(slots=True)
class GatewayV1Harness:
    root: Path
    service_path: Path
    runtime: GatewayRuntime
    snapshots: ConfigSnapshots
    gateway_server: grpc.aio.Server
    commander_server: grpc.aio.Server
    commander: FakeCommanderService
    specs: dict[str, ServerSpec]
    grpc_port: int
    commander_port: int
    _channel: grpc.aio.Channel
    pki: dict[str, object] | None = None

    def modbus_port(self, server_id: str) -> int:
        return self.specs[server_id].modbus_port

    def modbus_client(self, server_id: str) -> AsyncModbusTcpClient:
        return AsyncModbusTcpClient(
            "127.0.0.1",
            port=self.specs[server_id].modbus_port,
            timeout=5,
        )

    def gateway_stub(self):
        from contracts import industrial_gateway_pb2_grpc as irpc

        return irpc.IndustrialGatewayServiceStub(self._channel)

    def common_stub(self):
        from contracts import common_pb2_grpc as common_rpc

        return common_rpc.CommonServiceStub(self._channel)

    def mtls_channel(self, client_name: str) -> grpc.aio.Channel:
        """以指定 CN 的真实客户端证书打开独立 mTLS 通道（调用方负责关闭）。"""
        if self.pki is None:
            raise RuntimeError("PKI was not generated for this harness")
        ca = cast(Path, self.pki["ca"]).read_bytes()
        cert_path, key_path = cast(dict[str, tuple[Path, Path]], self.pki["clients"])[client_name]
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca,
            private_key=key_path.read_bytes(),
            certificate_chain=cert_path.read_bytes(),
        )
        return grpc.aio.secure_channel(
            f"127.0.0.1:{self.grpc_port}",
            credentials,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )

    async def stop(self) -> None:
        await self._channel.close()
        await self.runtime.stop()
        await self.gateway_server.stop(grace=0)
        await self.commander_server.stop(grace=0)


async def claim_for_injection(
    stub: Any,
    gateway_id: str,
    variable_ids: Sequence[str],
    *,
    owner_id: str = "acceptance-harness",
    lease_ms: int = 60_000,
) -> str:
    """通过正式 ClaimTargets RPC 获取真实租约，返回 UpdateVariables 所需 token。"""
    reply = await stub.ClaimTargets(
        ipb.ClaimTargetsRequest(
            gateway_id=gateway_id,
            owner_id=owner_id,
            variable_ids=list(variable_ids),
            action="ACQUIRE",
            lease_ms=lease_ms,
        )
    )
    assert reply.accepted, reply.error_code
    return reply.lease_token


async def start_gateway_v1(
    root: Path,
    specs: Sequence[ServerSpec],
    *,
    commander: FakeCommanderService | None = None,
    forward_queue_capacity: int = 1024,
    max_forward_inflight: int = 16,
    submit_deadline_ms: int = 5000,
    metrics_window_seconds: int = 60,
    mtls: dict[str, Sequence[str]] | None = None,
    bootstrap_cn: str | None = None,
    extra_clients: Sequence[str] = (),
    point_table: dict[str, dict[str, Any]] | None = None,
    simulation: dict[str, Any] | None = None,
) -> GatewayV1Harness:
    """启动真实 Commander 对端 + 真实 Gateway runtime/gRPC，不替换任何生产对象。

    mtls 非空时生成真实 PKI 并按 ``{CN: 权限集}`` 配置服务端；harness 自身
    管理通道使用 bootstrap_cn 对应客户端证书。
    """
    root.mkdir(parents=True, exist_ok=True)
    commander_port = reserve_port()
    grpc_port = reserve_port()
    commander = commander or FakeCommanderService()
    commander_server = grpc.aio.server()
    crpc.add_CommanderServiceServicer_to_server(commander, commander_server)
    commander_server.add_insecure_port(f"127.0.0.1:{commander_port}")
    await commander_server.start()

    pki: dict[str, object] | None = None
    if mtls is not None:
        client_names = set(mtls) | set(extra_clients)
        if bootstrap_cn is not None:
            client_names.add(bootstrap_cn)
        pki = write_mtls_pki(root, tuple(client_names))
    service_path = write_config_tree(
        root,
        specs,
        commander_endpoint=f"127.0.0.1:{commander_port}",
        grpc_port=grpc_port,
        forward_queue_capacity=forward_queue_capacity,
        max_forward_inflight=max_forward_inflight,
        submit_deadline_ms=submit_deadline_ms,
        metrics_window_seconds=metrics_window_seconds,
        permissions=mtls,
        mtls_enabled=mtls is not None,
        point_table=point_table,
        simulation=simulation,
    )
    config = load_gateway(service_path, project_root=root)
    runtime = assemble_gateway(config)
    snapshots = ConfigSnapshots(runtime, service_path, root)
    gateway_server = create_gateway_grpc_server(runtime, snapshots)
    await gateway_server.start()
    await runtime.start()
    if mtls is not None:
        assert pki is not None and bootstrap_cn is not None
        ca_path = cast(Path, pki["ca"])
        client_certs = cast(dict[str, tuple[Path, Path]], pki["clients"])
        ca = ca_path.read_bytes()
        cert_path, key_path = client_certs[bootstrap_cn]
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca,
            private_key=key_path.read_bytes(),
            certificate_chain=cert_path.read_bytes(),
        )
        channel = grpc.aio.secure_channel(
            f"127.0.0.1:{grpc_port}",
            credentials,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )
    else:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{grpc_port}")
    await channel.channel_ready()
    return GatewayV1Harness(
        root=root,
        service_path=service_path,
        runtime=runtime,
        snapshots=snapshots,
        gateway_server=gateway_server,
        commander_server=commander_server,
        commander=commander,
        specs={spec.server_id: spec for spec in specs},
        grpc_port=grpc_port,
        commander_port=commander_port,
        _channel=channel,
        pki=pki,
    )


def two_server_specs(
    *,
    control_enabled: bool = True,
    desired_state: str = "START",
) -> list[ServerSpec]:
    """gw1/gw2 两台独立设备 Server（unit 1/2，独立端口）。"""
    return [
        ServerSpec(
            server_id="gw1",
            device_id="D01",
            modbus_port=reserve_port(),
            slave_id=1,
            desired_state=desired_state,
            control_enabled=control_enabled,
        ),
        ServerSpec(
            server_id="gw2",
            device_id="D02",
            modbus_port=reserve_port(),
            slave_id=2,
            desired_state=desired_state,
            control_enabled=control_enabled,
        ),
    ]
