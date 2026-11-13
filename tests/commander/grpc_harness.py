"""Commander gRPC 测试共享栈：真实 mTLS PKI + build_commander + 安全端口。

被 integration 与 system 两层 conftest 复用：
  * EC P-256 CA 签发服务器/客户端证书（RFC8222 邮箱型 SAN 承载 caller 身份）；
  * ``grpc.ssl_server_credentials(require_client_auth=True)`` 仅开安全端口；
  * build_commander 组合根（真实 ConfigSnapshots/commit、EventRepository WAL）。
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree
from typing import Any

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.assembly import build_commander
from commander.infrastructure.grpc.server import register_services
from commander.infrastructure.grpc.snapshots import ConfigSnapshots
from contracts import commander_pb2_grpc, common_pb2_grpc
from core.application.protocol_registry import ProtocolRegistry
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.support.process import free_port

_REPO_CONFIGS = Path(__file__).resolve().parents[2] / "configs"

# §9.3.1 权限矩阵：query / control / reload
IDENTITIES = {
    "viewer": ("viewer@windhub.test", "viewer-console", ("query",)),
    "ops": ("ops@windhub.test", "ops-console", ("control", "query")),
    "deployer": ("deployer@windhub.test", "reload-deployer", ("reload", "query")),
}
STRANGER_IDENTITY = "stranger@windhub.test"


def _service_yaml(
    sqlite_path: Path,
    nats_url: str,
    *,
    stream: str,
    subject: str,
) -> str:
    return f"""\
schema_version: 1
file_version: 1.0.0
config_sources:
  instances:
    devices: config/instances/devices.yaml
  templates:
    root: config/templates
dispatcher:
  max_concurrency: 100
  max_pending_per_device: 10
  max_pending_global: 1000
  max_batch_size: 10
timeouts:
  queue_default_ms: 1000
  connect_ms: 3000
  write_default_ms: 3000
repository:
  sqlite_path: {sqlite_path}
  max_storage_bytes: 1073741824
  reserved_storage_bytes: 67108864
  retention_seconds: 1800
  cleanup_interval_seconds: 3600
  storage_warning_ratio: 0.8
  admission_stop_ratio: 0.9
audit:
  nats_servers: [{nats_url}]
  stream: {stream}
  subject: {subject}
  retry_base_ms: 100
  retry_max_ms: 5000
shutdown:
  grace_seconds: 10
"""


def _wt01_block(modbus_port: int, label: str) -> str:
    return f"""
  WT01:
    name: "Commander {label} Modbus WT01"
    enabled: true
    model:
      id: modbus_wtg
      version: 1.0.0
    endpoint:
      host: 127.0.0.1
      port: {modbus_port}
    overrides:
      task:
        desired_state: STOP
      protocol_options:
        word_order: big_endian
"""


_SETPOINT_TABLE = """\
schema_version: 1
template_id: modbus_wtg
file_version: 1.0.0
protocol: modbus
points:
  setpoint_power:
    business_point_id: setpoint_power
    point_groups: [all]
    address:
      register_type: holding
      address: 200
    data_type: float32
    unit: kilowatt
"""

_BUSINESS_POINTS = """\
schema_version: 1
file_version: 1.0.0
business_points:
  setpoint_power:
    data_type: float32
    unit: kilowatt
    description: Writable active power setpoint
"""

_MODELS_TABLE = """\
schema_version: 1
template_id: modbus_wtg
file_version: 1.0.0
name: Commander acceptance Modbus turbine
device_type_id: turbine
protocol:
  id: modbus_tcp
  version: 1.0.0
point_table:
  id: modbus_wtg
  version: 1.0.0
task:
  id: turbine_all
  version: 1.0.0
"""


@dataclass
class Pki:
    """真实 PKI：CA 与各身份证书的 PEM 字节访问。"""

    tmp: Path
    ca_cert: bytes

    def cert(self, name: str) -> bytes:
        return (self.tmp / f"{name}.crt").read_bytes()

    def key(self, name: str) -> bytes:
        return (self.tmp / f"{name}.key").read_bytes()


def build_pki(tmp_path: Path) -> Pki:
    """签发 CA、服务器证书、viewer/ops/deployer/双身份/陌生人证书及外部 CA 证书。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    not_before = base - timedelta(days=1)
    not_after = base + timedelta(days=365)

    def key() -> ec.EllipticCurvePrivateKey:
        return ec.generate_private_key(ec.SECP256R1())

    def pem(private: ec.EllipticCurvePrivateKey) -> bytes:
        return private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def issue(
        issuer_cert: x509.Certificate,
        issuer_key: ec.EllipticCurvePrivateKey,
        subject_private: ec.EllipticCurvePrivateKey,
        cn: str,
        emails: list[str],
        *,
        server: bool = False,
        serial: int,
    ) -> x509.Certificate:
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(issuer_cert.subject)
            .public_key(subject_private.public_key())
            .serial_number(serial)
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
        )
        names: list[x509.GeneralName] = [x509.RFC822Name(email) for email in emails]
        if server:
            names += [
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
        return builder.sign(issuer_key, hashes.SHA256())

    ca_private = key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WindHub Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_private.public_key())
        .serial_number(1)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_private, hashes.SHA256())
    )

    server_private = key()
    server_cert = issue(
        ca_cert, ca_private, server_private, "commander-server", [], server=True, serial=2
    )

    leaves: dict[str, tuple[x509.Certificate, ec.EllipticCurvePrivateKey]] = {}
    serial = 10
    leaf_specs = {
        "viewer": [IDENTITIES["viewer"][0]],
        "ops": [IDENTITIES["ops"][0]],
        "deployer": [IDENTITIES["deployer"][0]],
        "dual": [IDENTITIES["viewer"][0], IDENTITIES["ops"][0]],
        "stranger": [STRANGER_IDENTITY],
    }
    for name, emails in leaf_specs.items():
        private = key()
        leaves[name] = (
            issue(ca_cert, ca_private, private, f"client-{name}", emails, serial=serial),
            private,
        )
        serial += 1

    rogue_ca_private = key()
    rogue_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rogue CA")])
    rogue_ca = (
        x509.CertificateBuilder()
        .subject_name(rogue_name)
        .issuer_name(rogue_name)
        .public_key(rogue_ca_private.public_key())
        .serial_number(900)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(rogue_ca_private, hashes.SHA256())
    )
    rogue_private = key()
    rogue_cert = issue(
        rogue_ca,
        rogue_ca_private,
        rogue_private,
        "client-outsider",
        ["outsider@windhub.test"],
        serial=901,
    )

    def dump(name: str, cert: x509.Certificate, private: ec.EllipticCurvePrivateKey) -> None:
        (tmp_path / f"{name}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / f"{name}.key").write_bytes(pem(private))

    dump("ca", ca_cert, ca_private)
    dump("server", server_cert, server_private)
    for name, (cert, private) in leaves.items():
        dump(name, cert, private)
    dump("rogue-ca", rogue_ca, rogue_ca_private)
    dump("rogue", rogue_cert, rogue_private)
    return Pki(tmp=tmp_path, ca_cert=ca_cert.public_bytes(serialization.Encoding.PEM))


@dataclass
class GrpcStack:
    service: CommanderService
    snapshots: ConfigSnapshots
    target: str
    yaml_path: Path
    root: Path
    db_path: Path
    permissions: dict[str, tuple[str, frozenset[str]]]
    stream: str
    subject: str

    def channel(
        self,
        pki: Pki,
        *,
        client: str | None = "ops",
        no_client_cert: bool = False,
        ca: bytes | None = None,
    ) -> grpc.aio.Channel:
        """真实 TLS 客户端通道；client=None 且不提供证书时为无证书调用。"""
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca if ca is not None else pki.ca_cert,
            private_key=None if no_client_cert or client is None else pki.key(client),
            certificate_chain=None if no_client_cert or client is None else pki.cert(client),
        )
        return grpc.aio.secure_channel(
            self.target,
            credentials,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )

    def stubs(
        self, pki: Pki, *, client: str | None = "ops", **kwargs: Any
    ) -> tuple[common_pb2_grpc.CommonServiceStub, commander_pb2_grpc.CommanderServiceStub]:
        channel = self.channel(pki, client=client, **kwargs)
        return (
            common_pb2_grpc.CommonServiceStub(channel),
            commander_pb2_grpc.CommanderServiceStub(channel),
        )


def _permissions() -> dict[str, tuple[str, frozenset[str]]]:
    return {
        identity: (caller_id, frozenset(grants))
        for identity, caller_id, grants in IDENTITIES.values()
    }


async def build_stack(
    tmp_path: Path,
    pki: Pki,
    *,
    modbus_port: int | None = None,
    nats_url: str = "nats://127.0.0.1:1",
    stream_prefix: str = "CMD_IT",
    label: str = "integration",
    prepare: Callable[[Path], None] | None = None,
) -> tuple[GrpcStack, grpc.aio.Server, CommanderService]:
    """真实共享配置树 + build_commander + 仅安全端口的 gRPC 服务器。

    生产式目录：``<root>/service/commander.yaml`` 与
    ``<root>/service/config/{instances,templates}``；ConfigSnapshots 的
    快照根为 yaml 的上两级（``<root>``），snapshot 名形如
    ``service/config/instances/devices.yaml``。
    """
    service_dir = tmp_path / "service"
    root = service_dir / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    db_path = service_dir / "state" / "commander.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stream = f"{stream_prefix}_{uuid.uuid4().hex[:12]}"
    subject = f"commander.{label}.{uuid.uuid4().hex[:8]}"
    yaml_path = service_dir / "commander.yaml"
    yaml_path.write_text(
        _service_yaml(db_path, nats_url, stream=stream, subject=subject),
        encoding="utf-8",
    )
    if modbus_port is not None:
        devices_yaml = root / "instances" / "devices.yaml"
        devices_yaml.write_text(
            devices_yaml.read_text(encoding="utf-8") + _wt01_block(modbus_port, label),
            encoding="utf-8",
        )
        table = root / "templates" / "point_tables" / "point_table_modbus_wtg_v1.0.0.yaml"
        table.write_text(_SETPOINT_TABLE, encoding="utf-8")
        model = root / "templates" / "device_models" / "device_model_modbus_wtg_v1.0.0.yaml"
        model.write_text(_MODELS_TABLE, encoding="utf-8")
        business = root / "templates" / "business_points_standard.yaml
        text = business.read_text(encoding="utf-8")
        if "  setpoint_power:" not in text:
            business.write_text(
                text + _BUSINESS_POINTS.split("business_points:\n", 1)[1],
                encoding="utf-8",
            )
    if prepare is not None:
        prepare(service_dir)

    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    service = build_commander(service_config=yaml_path, protocol_registry=registry)
    await service.start()
    snapshots = ConfigSnapshots(service, yaml_path)
    permissions = _permissions()
    server = grpc.aio.server(maximum_concurrent_rpcs=100)
    register_services(server, service, permissions, snapshots)
    credentials = grpc.ssl_server_credentials(
        [(pki.key("server"), pki.cert("server"))],
        root_certificates=pki.ca_cert,
        require_client_auth=True,
    )
    port = free_port()
    bound = server.add_secure_port(f"127.0.0.1:{port}", credentials)
    assert bound, "gRPC secure port failed to bind"
    await server.start()
    stack = GrpcStack(
        service=service,
        snapshots=snapshots,
        target=f"127.0.0.1:{port}",
        yaml_path=yaml_path,
        root=tmp_path,
        db_path=db_path,
        permissions=permissions,
        stream=stream,
        subject=subject,
    )
    return stack, server, service
