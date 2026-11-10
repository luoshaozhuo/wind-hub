"""S3：Commander 查询权限隔离（真实 mTLS 双调用方 + gRPC + SQLite + Modbus）。

设计依据 Commander v1_24：command_id 与 request_id 两类查询都必须按证书
身份映射出的 caller 命名空间授权——

1. 调用方 A 提交命令后，A 用 command_id 查询成功；
2. 持 query 授权的调用方 B 查询 A 的 command_id 得到 NOT_FOUND，
   不泄露设备/点/状态等命令详情；
3. B 提交自己的命令后可正常查询；
4. A/B 使用同一 request_id 仍按身份隔离，GetRequestResult 边界一致；
5. 进程重启（真实 flock 释放、SQLite 重开、启动恢复）后隔离不失效；
6. 越权 NOT_FOUND 不影响正常控制、持久幂等等既有语义。

第二张客户端证书由测试 CA 在测试内即时签发，权限项运行期注册进服务器
_Authorized 持有的同一权限表，走完整生产授权与 caller 解析路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
from typing import Any
from uuid import uuid4

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.assembly import build_commander
from commander.infrastructure.grpc.server import register_services
from commander.infrastructure.grpc.snapshots import ConfigSnapshots
from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc, common_pb2
from core.application.protocol_registry import ProtocolRegistry
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.commander.grpc_harness import GrpcStack, Pki, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port

pytestmark = [pytest.mark.integration, pytest.mark.real_service, pytest.mark.security]

_SECOND_CALLER_EMAIL = "ops2@windhub.test"
_SECOND_CALLER_ID = "ops2-console"
_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED", "CANCELLED"}


def _issue_second_caller_cert(pki: Pki) -> tuple[bytes, bytes]:
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


def _request(revision: str, request_id: str, *, value: float = 42.5) -> pb.SubmitCommandRequest:
    return pb.SubmitCommandRequest(
        device_id="WT01",
        point_id="setpoint_power",
        value=common_pb2.ScalarValue(double_value=value),
        expected_revision=revision,
        request_id=request_id,
    )


async def _poll_terminal(commander: Any, request_id: str) -> Any:
    for _ in range(200):
        view = await commander.GetRequestResult(
            pb.GetRequestResultRequest(request_id=request_id), timeout=5
        )
        if view.state in _TERMINAL:
            return view
        await asyncio.sleep(0.05)
    raise AssertionError(f"request {request_id} never reached a terminal state")


async def _simulate_process_kill(server: grpc.aio.Server, service: CommanderService) -> None:
    """复刻 OS 回收：停入口、取消后台任务、真实关闭 SQLite 释放 flock。"""
    await server.stop(grace=5)
    watchdog = service._watchdog
    if watchdog is not None:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watchdog
    for task in tuple(service.executor._idle_tasks.values()):
        task.cancel()
    for task in tuple(service.executor._idle_tasks.values()):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    audit = service._audit
    if audit is not None and audit._task is not None:
        audit._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(audit._task), timeout=3.0)
    persistence = service._persistence
    if persistence is not None:
        with contextlib.suppress(Exception):
            await persistence.stop(timeout=5.0)
    loop = asyncio.get_running_loop()
    journal = service._journal
    writer = service._writer
    assert journal is not None and writer is not None
    await loop.run_in_executor(writer, journal.close)
    await asyncio.to_thread(writer.shutdown, wait=True)
    if service._buffer is not None:
        service._buffer.close()


async def _restart(
    stack: GrpcStack, pki: Pki
) -> tuple[GrpcStack, grpc.aio.Server, CommanderService]:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    service2 = build_commander(service_config=stack.yaml_path, protocol_registry=registry)
    await service2.start()
    snapshots2 = ConfigSnapshots(service2, stack.yaml_path)
    server2 = grpc.aio.server(maximum_concurrent_rpcs=100)
    register_services(server2, service2, stack.permissions, snapshots2)
    credentials = grpc.ssl_server_credentials(
        [(pki.key("server"), pki.cert("server"))],
        root_certificates=pki.ca_cert,
        require_client_auth=True,
    )
    port = free_port()
    assert server2.add_secure_port(f"127.0.0.1:{port}", credentials)
    await server2.start()
    restarted = GrpcStack(
        service=service2,
        snapshots=snapshots2,
        target=f"127.0.0.1:{port}",
        yaml_path=stack.yaml_path,
        root=stack.root,
        db_path=stack.db_path,
        permissions=stack.permissions,
        stream=stack.stream,
        subject=stack.subject,
    )
    return restarted, server2, service2


async def test_command_and_request_queries_are_caller_scoped(
    tmp_path, modbus_server: ModbusMockServer
) -> None:
    """A 的命令对 B 在两类查询 RPC 上均不可见；B 自己的命令可正常查询。"""
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-query-authz",
        stream_prefix="CMD_SRC_QA",
    )
    try:
        stack.permissions[_SECOND_CALLER_EMAIL] = (
            _SECOND_CALLER_ID,
            frozenset({"control", "query"}),
        )
        cert2, key2 = _issue_second_caller_cert(pki)
        _common, commander_a_raw = stack.stubs(pki, client="ops")
        commander_a: Any = commander_a_raw
        commander_b = _stub_for_identity(stack, pki, cert2, key2)
        revision = await _revision(commander_a)

        request_id = f"req-qa-{uuid4().hex}"
        submitted = await commander_a.SubmitCommand(_request(revision, request_id), timeout=5)
        view = await _poll_terminal(commander_a, request_id)
        assert view.state == "SUCCEEDED" and view.commands[0].io_settled is True

        # 1) A 用 command_id 查询自己的命令：成功且详情完整。
        own = await commander_a.GetCommandResult(
            pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
        )
        assert own.state == "SUCCEEDED"
        assert own.device_id == "WT01" and own.point_id == "setpoint_power"

        # 2) B 查 A 的 command_id：NOT_FOUND，错误详情只含 ID 不含命令内容。
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await commander_b.GetCommandResult(
                pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
            )
        assert exc.value.code() is grpc.StatusCode.NOT_FOUND
        assert "setpoint_power" not in (exc.value.details() or "")
        assert "WT01" not in (exc.value.details() or "")

        # 3) B 用 A 的 request_id 聚合查询：同样 NOT_FOUND（边界一致）。
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await commander_b.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
        assert exc.value.code() is grpc.StatusCode.NOT_FOUND

        # 4) B 提交自己的命令（同 request_id 是 B 命名空间内的新请求）并可查询。
        b_submitted = await commander_b.SubmitCommand(
            _request(revision, request_id, value=43.5), timeout=5
        )
        assert b_submitted.command_id != submitted.command_id
        b_view = await _poll_terminal(commander_b, request_id)
        assert [item.command_id for item in b_view.commands] == [b_submitted.command_id]
        b_command = await commander_b.GetCommandResult(
            pb.GetCommandResultRequest(command_id=b_submitted.command_id), timeout=5
        )
        assert b_command.state == "SUCCEEDED"

        # 5) A 仍只见自己的命令，B 的受理对 A 不可见。
        a_view = await commander_a.GetRequestResult(
            pb.GetRequestResultRequest(request_id=request_id), timeout=5
        )
        assert [item.command_id for item in a_view.commands] == [submitted.command_id]
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await commander_a.GetCommandResult(
                pb.GetCommandResultRequest(command_id=b_submitted.command_id), timeout=5
            )
        assert exc.value.code() is grpc.StatusCode.NOT_FOUND
    finally:
        await server.stop(grace=5)
        await service.stop()


async def test_query_authorization_survives_restart(
    tmp_path, modbus_server: ModbusMockServer
) -> None:
    """终态命令在进程重启后仍按 caller 隔离；持久层授权而非内存缓存。"""
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-query-authz-restart",
        stream_prefix="CMD_SRC_QAR",
    )
    crashed = False
    try:
        stack.permissions[_SECOND_CALLER_EMAIL] = (
            _SECOND_CALLER_ID,
            frozenset({"control", "query"}),
        )
        cert2, key2 = _issue_second_caller_cert(pki)
        _common, commander_a_raw = stack.stubs(pki, client="ops")
        commander_a: Any = commander_a_raw
        commander_b = _stub_for_identity(stack, pki, cert2, key2)
        revision = await _revision(commander_a)

        request_id = f"req-qar-{uuid4().hex}"
        submitted = await commander_a.SubmitCommand(_request(revision, request_id), timeout=5)
        await _poll_terminal(commander_a, request_id)
        writes_before = modbus_server.write_count
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await commander_b.GetCommandResult(
                pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
            )
        assert exc.value.code() is grpc.StatusCode.NOT_FOUND

        await _simulate_process_kill(server, service)
        crashed = True
        restarted, server2, service2 = await _restart(stack, pki)
        try:
            _common2, commander_a2_raw = restarted.stubs(pki, client="ops")
            commander_a2: Any = commander_a2_raw
            commander_b2 = _stub_for_identity(restarted, pki, cert2, key2)

            # 启动恢复未重发：Modbus 写入计数保持，受理快照恰为 A 的 1 条。
            assert modbus_server.write_count == writes_before
            assert service2.health().control_accepted_total == 1

            # A 的持久命令重启后可按 command_id/request_id 回查且终态保留。
            own = await commander_a2.GetCommandResult(
                pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
            )
            assert own.state == "SUCCEEDED"
            own_request = await commander_a2.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            assert [item.command_id for item in own_request.commands] == [submitted.command_id]

            # B 在重启后仍被拒绝：SQL 层 caller 过滤，不依赖进程内状态。
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await commander_b2.GetCommandResult(
                    pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
                )
            assert exc.value.code() is grpc.StatusCode.NOT_FOUND
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await commander_b2.GetRequestResult(
                    pb.GetRequestResultRequest(request_id=request_id), timeout=5
                )
            assert exc.value.code() is grpc.StatusCode.NOT_FOUND

            # 重启后控制面仍可用：B 受理自己的新命令并查询成功。
            new_id = f"req-qar-b-{uuid4().hex}"
            b_new = await commander_b2.SubmitCommand(
                _request(revision, new_id, value=44.5), timeout=5
            )
            await _poll_terminal(commander_b2, new_id)
            b_view = await commander_b2.GetCommandResult(
                pb.GetCommandResultRequest(command_id=b_new.command_id), timeout=5
            )
            assert b_view.state == "SUCCEEDED"
        finally:
            await server2.stop(grace=5)
            with contextlib.suppress(Exception):
                await service2.stop()
    finally:
        if not crashed:
            with contextlib.suppress(Exception):
                await server.stop(grace=1)
            with contextlib.suppress(Exception):
                await service.stop()
