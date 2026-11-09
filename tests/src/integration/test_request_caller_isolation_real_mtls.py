"""语义⑪：gRPC/mTLS 边界的调用方命名空间隔离（真实证书签发 + 真实链路）。

组件层已用 caller_id 字符串证明命名空间隔离；本测试在其之上补齐边界证据：
两个由同一 CA 真实签发、各自独立的 mTLS 客户端身份（ops-console 与测试内
签发的 ops2-console，均持 control/query 授权）使用同一 request_id 提交
同一载荷时，服务端必须按证书身份映射出两个 caller 命名空间——

1. 两次受理产生两个不同 command_id，Modbus 从站各真实写入一次；
2. 每个调用方只能按自己的命名空间聚合回查，对方的 request_id 明确
   NOT_FOUND，而非返回空投影或越权数据；
3. 同身份同 ID 重放仍命中自己的命名空间，返回原 command_id。

证书在测试内用 build_pki 落盘的 CA 密钥即时签发，不预生成静态资产；
权限表是服务器 _Authorized 持有的同一 dict 对象，运行期注册第二个
调用方，走的仍是生产授权与 caller 解析代码路径。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from uuid import uuid4

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc, common_pb2
from tests.commander.grpc_harness import GrpcStack, Pki, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

_SECOND_CALLER_EMAIL = "ops2@windhub.test"
_SECOND_CALLER_ID = "ops2-console"


def _issue_second_control_cert(pki: Pki) -> tuple[bytes, bytes]:
    """用测试 CA 真实签发第二个 control 调用方的客户端证书（RFC8222 邮箱 SAN）。"""
    ca_cert = x509.load_pem_x509_certificate((pki.tmp / "ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key((pki.tmp / "ca.key").read_bytes(), password=None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    private_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "client-ops2")]))
        .issuer_name(ca_cert.subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(_SECOND_CALLER_EMAIL)]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _stub_for_identity(stack: GrpcStack, pki: Pki, cert: bytes, key: bytes) -> Any:
    credentials = grpc.ssl_channel_credentials(
        root_certificates=pki.ca_cert,
        private_key=key,
        certificate_chain=cert,
    )
    channel = grpc.aio.secure_channel(
        stack.target,
        credentials,
        options=(("grpc.ssl_target_name_override", "localhost"),),
    )
    return commander_pb2_grpc.CommanderServiceStub(channel)


async def _revision(commander: Any) -> str:
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    return str(next(d.revision for d in devices.devices if d.device_id == "WT01"))


def _request(revision: str, request_id: str) -> pb.SubmitCommandRequest:
    return pb.SubmitCommandRequest(
        device_id="WT01",
        point_id="setpoint_power",
        value=common_pb2.ScalarValue(double_value=42.5),
        expected_revision=revision,
        request_id=request_id,
    )


async def test_same_request_id_isolated_across_mtls_callers(
    tmp_path, modbus_server: ModbusMockServer
) -> None:
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-caller-isolation",
        stream_prefix="CMD_SRC_ISO",
    )
    try:
        # 运行期注册第二个 control 调用方；与服务器 _Authorized 持有同一 dict。
        stack.permissions[_SECOND_CALLER_EMAIL] = (
            _SECOND_CALLER_ID,
            frozenset({"control", "query"}),
        )
        cert2, key2 = _issue_second_control_cert(pki)
        _common, commander_a = stack.stubs(pki, client="ops")
        commander_b = _stub_for_identity(stack, pki, cert2, key2)

        revision = await _revision(commander_a)
        request_id = f"req-iso-{uuid4().hex}"
        before = modbus_server.write_count

        first = await commander_a.SubmitCommand(_request(revision, request_id), timeout=5)
        second = await commander_b.SubmitCommand(_request(revision, request_id), timeout=5)
        # 两个证书身份是两个 caller 命名空间：同 ID 同载荷也必须各自独立受理。
        assert first.command_id != second.command_id
        assert first.request_id == request_id == second.request_id

        # 双方各自重放只命中自己的命名空间，command_id 稳定不变。
        replay_a = await commander_a.SubmitCommand(_request(revision, request_id), timeout=5)
        replay_b = await commander_b.SubmitCommand(_request(revision, request_id), timeout=5)
        assert replay_a.command_id == first.command_id
        assert replay_b.command_id == second.command_id

        # 聚合回查按调用方隔离：各自只见自己的命令，对方命名空间显式 NOT_FOUND。
        # 受理与现场写异步分离，先轮询到终态再裁决写入计数。
        terminal = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED", "CANCELLED"}
        view_a = None
        view_b = None
        for _ in range(200):
            view_a = await commander_a.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            view_b = await commander_b.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            if view_a.state in terminal and view_b.state in terminal:
                break
            await asyncio.sleep(0.05)
        assert view_a is not None and view_a.state == "SUCCEEDED"
        assert view_b is not None and view_b.state == "SUCCEEDED"
        assert [item.command_id for item in view_a.commands] == [first.command_id]
        assert [item.command_id for item in view_b.commands] == [second.command_id]

        # 两个独立受理各自真实写入一次，重放不产生额外现场写。
        assert modbus_server.write_count - before == 2
    finally:
        await server.stop(grace=5)
        await service.stop()
