"""可信恢复的独立 HTTPS/mTLS 证明方、真实 gRPC 与真实 SQLite 集成矩阵。

证明方运行在独立 OS 进程并维护受控模拟 fencing 状态机。成功仅证明
Commander 集成行为，不构成真实生产设备或部署侧物理隔离证据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest

from commander.application.persistence import BufferedJournal
from commander.application.service_lifecycle import CommanderService, ServiceState
from commander.domain.command import CommandSpec
from commander.infrastructure.event_repository import EventRepository
from commander.infrastructure.recovery_authority import IsolationAuthorityClient
from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc, common_pb2
from tests.commander.grpc_harness import GrpcStack, Pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.fencing_authority import FencingAuthorityProcess
from tests.support.process import free_port

from . import test_audit_shutdown_real as audit_shutdown
from .test_audit_shutdown_real import (
    _wait_nats_ready,
    _wait_port,
)
from .test_nats_jetstream_real import _connect, _fetch_all

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.real_service]

_SECRET = b"trusted-recovery-sensitive-attestation-\x00\xff"
_NEW_INSTANCE = "new-commander-instance"


@pytest.fixture(scope="module")
def trusted_nats_container() -> Iterator[tuple[str, str]]:
    yield from audit_shutdown.nats_container.__wrapped__()


@dataclass
class RecoveryContext:
    stack: GrpcStack
    server: grpc.aio.Server
    service: CommanderService
    revisions: dict[str, str]
    command_ids: tuple[str, ...]


@pytest.fixture
def authority_factory(tmp_path: Path, pki: Pki) -> Iterator[Callable[..., FencingAuthorityProcess]]:
    processes: list[FencingAuthorityProcess] = []

    def start(
        scenario: dict[str, Any] | None = None,
        *,
        server_name: str = "authority",
    ) -> FencingAuthorityProcess:
        process = FencingAuthorityProcess(
            tmp_path / "authority-processes",
            ca_cert=pki.tmp / "ca.crt",
            server_cert=pki.tmp / f"{server_name}.crt",
            server_key=pki.tmp / f"{server_name}.key",
            scenario=scenario,
        )
        processes.append(process)
        return process

    try:
        yield start
    finally:
        for process in processes:
            process.close()


def _settings(
    pki: Pki,
    authority: FencingAuthorityProcess,
    *,
    trusted_ca: str = "ca",
    client_name: str = "ops",
    timeout: float = 1.0,
) -> dict[str, object]:
    return {
        "url": authority.url,
        "instance_id": _NEW_INSTANCE,
        "ca_cert": str(pki.tmp / f"{trusted_ca}.crt"),
        "client_cert": str(pki.tmp / f"{client_name}.crt"),
        "client_key": str(pki.tmp / f"{client_name}.key"),
        "timeout_seconds": timeout,
    }


@asynccontextmanager
async def _recovery_context(
    root: Path,
    pki: Pki,
    modbus_server: Any,
    recovery: dict[str, object],
    *,
    devices: tuple[str, ...] = ("WT01",),
    database_source: Path | None = None,
    nats_url: str = "nats://127.0.0.1:1",
    device_ports: dict[str, int] | None = None,
) -> AsyncIterator[RecoveryContext]:
    command_ids: tuple[str, ...] = ()

    def prepare(service_dir: Path) -> None:
        nonlocal command_ids
        db_path = service_dir / "state" / "commander.db"
        if database_source is not None:
            # SQLite online backup 包含尚未 checkpoint 的 WAL，适用于进程崩溃后的重启。
            source = sqlite3.connect(database_source)
            target = sqlite3.connect(db_path)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            return
        repository = EventRepository(db_path)
        journal = BufferedJournal()
        accepted = journal.accept(
            [CommandSpec(device, "setpoint_power", 10.0, "recovery-seed") for device in devices],
            request_id="req-before-trusted-recovery",
            caller_id="ops-console",
        )
        command_ids = tuple(command.command_id for command in accepted)
        repository.append_events(journal.snapshot(100))
        repository.close()

    stream = f"CMD_TR_{uuid4().hex[:12]}"
    subject = f"commander.trusted.{uuid4().hex[:8]}"
    if nats_url != "nats://127.0.0.1:1":
        connection = await _connect(nats_url)
        try:
            await connection.jetstream(timeout=5).add_stream(name=stream, subjects=[subject])
        finally:
            await connection.drain()
    stack, server, service = await build_stack(
        root,
        pki,
        modbus_port=modbus_server.port,
        nats_url=nats_url,
        prepare=prepare,
        recovery=recovery,
        device_ids=devices,
        device_ports=device_ports,
        audit_stream=stream,
        audit_subject=subject,
        label="trusted-recovery",
    )
    try:
        channel = stack.channel(pki, client="ops")
        try:
            listed = await commander_pb2_grpc.CommanderServiceStub(channel).ListDevices(
                pb.ListDevicesRequest(), timeout=5
            )
        finally:
            await channel.close()
        revisions = {device.device_id: device.revision for device in listed.devices}
        assert service.executor.blocked_devices() == frozenset(devices)
        assert service.health().state is ServiceState.DEGRADED
        yield RecoveryContext(stack, server, service, revisions, command_ids)
    finally:
        await server.stop(grace=5)
        await service.stop()


async def _recover(
    context: RecoveryContext,
    pki: Pki,
    *,
    device_id: str = "WT01",
    request_id: str = "req-recovery-a",
    evidence: bytes = _SECRET,
) -> pb.RecoverDeviceControlResponse:
    channel = context.stack.channel(pki, client="ops")
    try:
        return await commander_pb2_grpc.CommanderServiceStub(channel).RecoverDeviceControl(
            pb.RecoverDeviceControlRequest(
                device_id=device_id,
                expected_revision=context.revisions[device_id],
                request_id=request_id,
                isolation_attestation=evidence,
            ),
            timeout=8,
        )
    finally:
        await channel.close()


async def _assert_unknown(context: RecoveryContext) -> None:
    for command_id in context.command_ids:
        result = await context.service.control.get_command(command_id)
        assert result is not None and result.outcome is not None
        assert result.state.value == result.outcome.value == "UNKNOWN"


def _decision_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM recovery_decisions").fetchone()[0])
    finally:
        connection.close()


def _assert_unknown_durable(path: Path, command_ids: tuple[str, ...]) -> None:
    connection = sqlite3.connect(path, timeout=5)
    try:
        for command_id in command_ids:
            row = connection.execute(
                "SELECT state,outcome FROM command_events WHERE command_id=? "
                "ORDER BY event_seq DESC LIMIT 1",
                (command_id,),
            ).fetchone()
            assert row == ("UNKNOWN", "UNKNOWN")
    finally:
        connection.close()


def _recovery_epochs(path: Path) -> list[tuple[str, int]]:
    connection = sqlite3.connect(path, timeout=5)
    try:
        return [
            (str(request_id), int(epoch))
            for request_id, epoch in connection.execute(
                "SELECT request_id,fencing_epoch FROM recovery_decisions "
                "WHERE device_id='WT01' ORDER BY fencing_epoch"
            )
        ]
    finally:
        connection.close()


def _pending_audit_counts(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path, timeout=5)
    try:
        commands = connection.execute(
            "SELECT COUNT(*) FROM command_events WHERE publish_state='PENDING'"
        ).fetchone()[0]
        decisions = connection.execute(
            "SELECT COUNT(*) FROM recovery_decisions WHERE publish_state='PENDING'"
        ).fetchone()[0]
        return int(commands), int(decisions)
    finally:
        connection.close()


async def _wait_audit_drained(path: Path, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while _pending_audit_counts(path) != (0, 0):
            await asyncio.sleep(0.05)


async def _submit_and_wait(context: RecoveryContext, pki: Pki, request_id: str) -> str:
    channel = context.stack.channel(pki, client="ops")
    try:
        stub = commander_pb2_grpc.CommanderServiceStub(channel)
        submitted = await stub.SubmitCommand(
            pb.SubmitCommandRequest(
                device_id="WT01",
                point_id="setpoint_power",
                value=common_pb2.ScalarValue(double_value=42.5),
                expected_revision=context.revisions["WT01"],
                request_id=request_id,
            ),
            timeout=5,
        )
        assert submitted.accepted is True
        async with asyncio.timeout(10):
            while True:
                result = await stub.GetCommandResult(
                    pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
                )
                if result.state in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}:
                    assert result.state == "SUCCEEDED"
                    assert result.outcome == "CONFIRMED_SUCCESS"
                    assert result.io_settled is True
                    return str(submitted.command_id)
                await asyncio.sleep(0.05)
    finally:
        await channel.close()


def _crash_after_commit_process(
    messages: Any,
    root: Path,
    pki: Pki,
    modbus_port: int,
    recovery: dict[str, object],
) -> None:
    """真实 Commander 子进程：SQLite 提交返回后、本地解除前立即退出。"""

    async def run() -> None:
        command_id = ""

        def prepare(service_dir: Path) -> None:
            nonlocal command_id
            repository = EventRepository(service_dir / "state" / "commander.db")
            journal = BufferedJournal()
            accepted = journal.accept(
                [CommandSpec("WT01", "setpoint_power", 10.0, "recovery-seed")],
                request_id="req-before-crash",
                caller_id="ops-console",
            )
            command_id = accepted[0].command_id
            repository.append_events(journal.snapshot(100))
            repository.close()

        stack, _server, service = await build_stack(
            root,
            pki,
            modbus_port=modbus_port,
            prepare=prepare,
            recovery=recovery,
            label="trusted-recovery-crash",
        )
        revision = service.config_manager.device_revision("WT01")
        assert revision is not None
        journal = vars(service)["_journal"]
        original = journal.commit_recovery

        def commit_then_exit(**kwargs: Any) -> str:
            decision_id = original(**kwargs)
            messages.send({"event": "committed", "decision_id": decision_id})
            os._exit(73)

        journal.commit_recovery = commit_then_exit
        messages.send(
            {
                "event": "ready",
                "target": stack.target,
                "db_path": str(stack.db_path),
                "revision": revision,
                "command_id": command_id,
            }
        )
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except BaseException as exc:
        messages.send({"event": "process-error", "error": type(exc).__name__})
        raise


async def _receive_process_message(messages: Any, timeout: float = 20.0) -> dict[str, Any]:
    assert await asyncio.to_thread(messages.poll, timeout), "Commander child sent no message"
    value = await asyncio.to_thread(messages.recv)
    assert isinstance(value, dict)
    assert value.get("event") != "process-error", value
    return value


async def test_external_fencing_success_unblocks_without_rewriting_unknown(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        response = await _recover(context, pki)
        assert response.state == "UNBLOCKED"
        assert response.evidence_digest == hashlib.sha256(_SECRET).hexdigest()
        assert response.decision_id
        assert context.service.executor.blocked_devices() == frozenset()
        assert _decision_count(context.stack.db_path) == 1
        await _assert_unknown(context)
        assert modbus_server.write_count == 0
        fenced = authority.state()["devices"]["WT01"]
        assert fenced["old_fenced"] is True
        assert fenced["owner"] == _NEW_INSTANCE
        # 首次裁决 epoch=1；COMMIT 后同一控制权的续期核验增长到 2。
        assert fenced["epoch"] == 2
        assert len(authority.state()["requests"]) == 2


@pytest.mark.parametrize(
    ("scenario", "case_id"),
    [
        ({"status": 403}, "invalid-proof"),
        ({"overrides": {"expires_at": "2000-01-01T00:00:00+00:00"}}, "expired"),
        ({"overrides": {"revoked": True}}, "revoked"),
        ({"omit": ["revoked"]}, "revoked-missing"),
        ({"overrides": {"revoked": "false"}}, "revoked-string"),
        ({"overrides": {"revoked": 0}}, "revoked-zero"),
        ({"overrides": {"revoked": 1}}, "revoked-one"),
        ({"overrides": {"revoked": None}}, "revoked-null"),
        ({"overrides": {"fencing_epoch": 0}}, "zero-epoch"),
        ({"overrides": {"device_id": "WT02"}}, "device-mismatch"),
        ({"overrides": {"config_revision": "other-revision"}}, "revision-mismatch"),
        ({"overrides": {"old_instance": _NEW_INSTANCE}}, "old-instance-mismatch"),
        ({"overrides": {"new_instance": "other-instance"}}, "new-instance-mismatch"),
        ({"overrides": {"caller_id": "other-caller"}}, "caller-mismatch"),
        ({"overrides": {"request_id": "other-request"}}, "request-id-mismatch"),
        ({"overrides": {"isolated": False}}, "not-isolated"),
        ({"overrides": {"fencing_enforced": False}}, "fencing-not-enforced"),
        ({"mode": "non-json"}, "non-json"),
        ({"mode": "oversized"}, "oversized-response"),
        ({"mode": "redirect"}, "redirect"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_invalid_or_untrusted_authority_response_never_unblocks(
    scenario: dict[str, Any],
    case_id: str,
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    caplog: pytest.LogCaptureFixture,
) -> None:
    del case_id
    authority = authority_factory(scenario)
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert context.service.health().state is ServiceState.DEGRADED
        await _assert_unknown(context)
        assert modbus_server.write_count == 0
        assert _decision_count(context.stack.db_path) == 0
        assert _SECRET.decode("utf-8", errors="ignore") not in caplog.text


@pytest.mark.parametrize("server_name", ["rogue-authority", "wrong-host"])
async def test_authority_ca_or_hostname_mismatch_fails_closed(
    server_name: str,
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory(server_name=server_name)
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == 0


async def test_invalid_commander_client_certificate_fails_closed(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority, client_name="rogue")
    ) as context:
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})


async def test_authority_timeout_fails_closed(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory({"delay_seconds": 3})
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority, timeout=0.2)
    ) as context:
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == 0


async def test_unavailable_authority_fails_closed(
    tmp_path: Path, pki: Pki, modbus_server: Any
) -> None:
    settings = {
        "url": "https://127.0.0.1:1/verify-and-fence",
        "instance_id": _NEW_INSTANCE,
        "ca_cert": str(pki.tmp / "ca.crt"),
        "client_cert": str(pki.tmp / "ops.crt"),
        "client_key": str(pki.tmp / "ops.key"),
        "timeout_seconds": 0.2,
    }
    async with _recovery_context(tmp_path, pki, modbus_server, settings) as context:
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})


async def test_same_request_retry_is_idempotent_and_different_content_conflicts(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        first = await _recover(context, pki)
        second = await _recover(context, pki)
        assert second.decision_id == first.decision_id
        assert _decision_count(context.stack.db_path) == 1
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await _recover(context, pki, evidence=b"different-evidence")
        assert exc_info.value.code() is grpc.StatusCode.ALREADY_EXISTS
        # 首次恢复核验两次，解除后的幂等重试实时核验一次；冲突不再调用证明方。
        assert len(authority.state()["requests"]) == 3


async def test_different_devices_and_same_device_recovery_races_are_safe(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    second_modbus = ModbusMockServer(port=free_port())
    await second_modbus.start()
    ports = {"WT01": modbus_server.port, "WT02": second_modbus.port}
    try:
        async with _recovery_context(
            tmp_path,
            pki,
            modbus_server,
            _settings(pki, authority),
            devices=("WT01", "WT02"),
            device_ports=ports,
        ) as context:
            different = await asyncio.gather(
                _recover(context, pki, device_id="WT01", request_id="req-wt01"),
                _recover(context, pki, device_id="WT02", request_id="req-wt02"),
            )
            assert [response.state for response in different] == ["UNBLOCKED", "UNBLOCKED"]
            assert _decision_count(context.stack.db_path) == 2

        # 新进程重新阻断后，同设备两个不同 request_id 只能有一个真实解除路径。
        authority.configure({"overrides": {"token_id": "second-generation-token"}})
        async with _recovery_context(
            tmp_path / "restart",
            pki,
            modbus_server,
            _settings(pki, authority),
            devices=("WT01", "WT02"),
            database_source=context.stack.db_path,
            device_ports=ports,
        ) as restarted:
            same_device = await asyncio.gather(
                _recover(restarted, pki, request_id="req-race-1"),
                _recover(restarted, pki, request_id="req-race-2"),
                return_exceptions=True,
            )
            successes = [
                item for item in same_device if isinstance(item, pb.RecoverDeviceControlResponse)
            ]
            failures = [item for item in same_device if isinstance(item, grpc.aio.AioRpcError)]
            assert len(successes) == 1 and len(failures) == 1
            assert restarted.service.executor.blocked_devices() == frozenset({"WT02"})
            assert _decision_count(restarted.stack.db_path) == 3
            epochs = [epoch for _request_id, epoch in _recovery_epochs(restarted.stack.db_path)]
            assert len(epochs) == 2 and epochs[0] < epochs[1]
    finally:
        await second_modbus.stop()


async def test_released_old_request_cannot_bypass_new_epoch_after_restart(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        command_ids = context.command_ids
        database_path = context.stack.db_path
        await _recover(context, pki, request_id="req-old-epoch")

    # 持久化新一代裁决并再次重启后，旧请求的相同 token 也不能复用历史授权。
    authority.configure({})
    async with _recovery_context(
        tmp_path / "new-epoch",
        pki,
        modbus_server,
        _settings(pki, authority),
        database_source=database_path,
    ) as context:
        current = await _recover(context, pki, request_id="req-current-epoch")
        repeated = await _recover(context, pki, request_id="req-current-epoch")
        assert repeated.decision_id == current.decision_id
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await _recover(context, pki, request_id="req-old-epoch")
        assert exc_info.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert context.service.executor.blocked_devices() == frozenset()
        assert _decision_count(context.stack.db_path) == 2
        epochs = _recovery_epochs(context.stack.db_path)
        assert [request_id for request_id, _epoch in epochs] == [
            "req-old-epoch",
            "req-current-epoch",
        ]
        assert epochs[0][1] < epochs[1][1]
        _assert_unknown_durable(context.stack.db_path, command_ids)
        assert modbus_server.write_count == 0


async def test_fencing_success_but_sqlite_insert_failure_rolls_back_and_stays_blocked(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        connection = sqlite3.connect(context.stack.db_path, timeout=5)
        try:
            connection.executescript(
                "CREATE TRIGGER inject_recovery_commit_failure "
                "BEFORE INSERT ON recovery_decisions BEGIN "
                "SELECT RAISE(ABORT, 'injected recovery storage failure'); END;"
            )
        finally:
            connection.close()
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert authority.state()["devices"]["WT01"]["old_fenced"] is True
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == 0
        await _assert_unknown(context)


async def test_stopped_audit_worker_rejects_recovery_instead_of_unblocking(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    trusted_nats_container,
) -> None:
    authority = authority_factory()
    nats_url, _container = trusted_nats_container
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority), nats_url=nats_url
    ) as context:
        audit = vars(context.service)["_audit"]
        assert audit is not None
        assert await audit.stop(timeout=5.0) is True
        assert audit.operational is False
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == 0


@pytest.mark.docker
@pytest.mark.parametrize("stop_after_verification", [1, 2], ids=["before-commit", "before-release"])
async def test_audit_worker_stop_during_recovery_never_unblocks(
    stop_after_verification: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    trusted_nats_container,
) -> None:
    authority = authority_factory()
    nats_url, _container = trusted_nats_container
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority), nats_url=nats_url
    ) as context:
        audit = vars(context.service)["_audit"]
        assert audit is not None
        original = IsolationAuthorityClient.verify_and_fence
        calls = 0

        async def verify_then_stop(client: IsolationAuthorityClient, **kwargs: Any) -> Any:
            nonlocal calls
            proof = await original(client, **kwargs)
            calls += 1
            if calls == stop_after_verification:
                assert await audit.stop(timeout=5.0) is True
                assert audit.operational is False
            return proof

        monkeypatch.setattr(IsolationAuthorityClient, "verify_and_fence", verify_then_stop)
        with pytest.raises(grpc.aio.AioRpcError):
            await _recover(context, pki)
        assert calls == stop_after_verification
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == stop_after_verification - 1
        await _assert_unknown(context)
        assert modbus_server.write_count == 0


@pytest.mark.parametrize(
    ("initial_scenario", "second_scenario", "case_id"),
    [
        ({}, {"overrides": {"revoked": True}}, "revoked"),
        (
            {},
            {"overrides": {"new_instance": "old-instance", "owner": "old-instance"}},
            "old-owner",
        ),
        ({}, {"overrides": {"token_id": "rotated-token"}}, "token-change"),
        (
            {"overrides": {"fencing_epoch": 10}},
            {"overrides": {"fencing_epoch": 9}},
            "epoch-rollback",
        ),
        (
            {},
            {"overrides": {"expires_at": "2000-01-01T00:00:00+00:00"}},
            "expired",
        ),
        ({}, {"delay_seconds": 3}, "timeout"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_commit_window_rechecks_live_fencing_before_release(
    initial_scenario: dict[str, Any],
    second_scenario: dict[str, Any],
    case_id: str,
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory(initial_scenario)
    async with _recovery_context(
        tmp_path,
        pki,
        modbus_server,
        _settings(pki, authority, timeout=0.2 if case_id == "timeout" else 1.0),
    ) as context:
        journal = vars(context.service)["_journal"]
        original = journal.commit_recovery
        committed = threading.Event()
        proceed = threading.Event()

        def delayed_commit(**kwargs: Any) -> str:
            decision_id = original(**kwargs)
            committed.set()
            assert proceed.wait(timeout=5), "test did not release delayed commit"
            return decision_id

        journal.commit_recovery = delayed_commit
        recovery = asyncio.create_task(_recover(context, pki))
        try:
            assert await asyncio.to_thread(committed.wait, 5)
            authority.configure(second_scenario)
            proceed.set()
            with pytest.raises(grpc.aio.AioRpcError):
                await recovery
            expected_requests = 1 if case_id == "timeout" else 2
            assert len(authority.state()["requests"]) == expected_requests
            assert context.service.executor.blocked_devices() == frozenset({"WT01"})
            assert _decision_count(context.stack.db_path) == 1
            await _assert_unknown(context)
            assert modbus_server.write_count == 0
            if case_id == "old-owner":
                assert authority.state()["devices"]["WT01"]["owner"] == "old-instance"
        finally:
            proceed.set()
            if not recovery.done():
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)


@pytest.mark.docker
async def test_same_request_after_commit_window_failure_accepts_renewed_epoch(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    trusted_nats_container,
) -> None:
    authority = authority_factory()
    nats_url, _container = trusted_nats_container
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority), nats_url=nats_url
    ) as context:
        journal = vars(context.service)["_journal"]
        original = journal.commit_recovery
        committed = threading.Event()
        proceed = threading.Event()
        committed_decision_ids: list[str] = []

        def delayed_commit(**kwargs: Any) -> str:
            decision_id = original(**kwargs)
            committed_decision_ids.append(decision_id)
            committed.set()
            assert proceed.wait(timeout=5), "test did not release delayed commit"
            return decision_id

        journal.commit_recovery = delayed_commit
        recovery = asyncio.create_task(_recover(context, pki))
        try:
            assert await asyncio.to_thread(committed.wait, 5)
            authority.configure({"overrides": {"revoked": True}})
            proceed.set()
            with pytest.raises(grpc.aio.AioRpcError):
                await recovery
        finally:
            proceed.set()
            if not recovery.done():
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == 1

        # 同一请求身份、证据和 token 的合法续期不是异内容冲突，应重新核验后解除。
        authority.configure({})
        journal.commit_recovery = original
        response = await _recover(context, pki)
        assert response.state == "UNBLOCKED"
        assert response.decision_id == committed_decision_ids[0]
        assert context.service.executor.blocked_devices() == frozenset()
        repeated = await _recover(context, pki)
        assert repeated.decision_id == response.decision_id
        assert _decision_count(context.stack.db_path) == 1
        assert _recovery_epochs(context.stack.db_path) == [("req-recovery-a", 1)]
        await _assert_unknown(context)
        assert modbus_server.write_count == 0

        await _wait_audit_drained(context.stack.db_path)
        connection = await _connect(nats_url)
        try:
            payloads = await _fetch_all(
                connection.jetstream(timeout=5), context.stack.stream, context.stack.subject, 3
            )
        finally:
            await connection.drain()
        events = [json.loads(payload) for payload in payloads]
        recovery_events = [
            event for event in events if event["event_type"] == "RECOVERY_FENCE_COMMITTED"
        ]
        assert len(recovery_events) == 1
        assert recovery_events[0]["event_id"] == response.decision_id
        assert recovery_events[0]["fencing_epoch"] == 1


async def test_superseded_request_cannot_release_with_renewed_epoch(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        journal = vars(context.service)["_journal"]
        original = journal.commit_recovery

        async def commit_without_release(request_id: str) -> None:
            committed = threading.Event()
            proceed = threading.Event()

            def delayed_commit(**kwargs: Any) -> str:
                decision_id = original(**kwargs)
                committed.set()
                assert proceed.wait(timeout=5), "test did not release delayed commit"
                return decision_id

            journal.commit_recovery = delayed_commit
            authority.configure({})
            recovery = asyncio.create_task(_recover(context, pki, request_id=request_id))
            try:
                assert await asyncio.to_thread(committed.wait, 5)
                authority.configure({"overrides": {"revoked": True}})
                proceed.set()
                with pytest.raises(grpc.aio.AioRpcError):
                    await recovery
            finally:
                proceed.set()
                journal.commit_recovery = original
                if not recovery.done():
                    recovery.cancel()
                    await asyncio.gather(recovery, return_exceptions=True)

        await commit_without_release("req-old-epoch")
        await commit_without_release("req-newer-epoch")
        epochs = _recovery_epochs(context.stack.db_path)
        assert [request_id for request_id, _epoch in epochs] == [
            "req-old-epoch",
            "req-newer-epoch",
        ]
        assert epochs[0][1] < epochs[1][1]

        authority.configure({})
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await _recover(context, pki, request_id="req-old-epoch")
        assert exc_info.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert context.service.executor.blocked_devices() == frozenset({"WT01"})
        assert _decision_count(context.stack.db_path) == 2
        await _assert_unknown(context)
        assert modbus_server.write_count == 0


@pytest.mark.docker
async def test_real_jetstream_commit_event_ready_transition_and_zero_replay(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    trusted_nats_container,
) -> None:
    authority = authority_factory()
    nats_url, _container = trusted_nats_container
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority), nats_url=nats_url
    ) as context:
        await _wait_audit_drained(context.stack.db_path)
        response = await _recover(context, pki)
        assert response.state == "UNBLOCKED"
        await _wait_audit_drained(context.stack.db_path)
        async with asyncio.timeout(8):
            while not context.service.health().ready:
                await asyncio.sleep(0.05)
        health = context.service.health()
        assert health.state is ServiceState.READY
        assert health.blocked_devices == ()
        assert health.pending_commands == 0
        assert health.inflight_io == 0

        new_command_id = await _submit_and_wait(context, pki, "req-after-recovery")
        assert new_command_id != context.command_ids[0]
        await _assert_unknown(context)
        assert modbus_server.write_count == 1
        await _wait_audit_drained(context.stack.db_path)

        connection = await _connect(nats_url)
        try:
            jetstream = connection.jetstream(timeout=5)
            async with asyncio.timeout(15):
                while (await jetstream.stream_info(context.stack.stream)).state.messages < 7:
                    await asyncio.sleep(0.05)
            payloads = await _fetch_all(jetstream, context.stack.stream, context.stack.subject, 7)
        finally:
            await connection.drain()
        events = [json.loads(payload) for payload in payloads]
        assert len({event["event_id"] for event in events}) == 7
        assert [event["event_type"] for event in events] == [
            "ACCEPTED",
            "COMPLETED",
            "RECOVERY_FENCE_COMMITTED",
            "ACCEPTED",
            "CONNECTING",
            "EXECUTING",
            "COMPLETED",
        ]
        assert events[-1]["command_id"] == new_command_id
        recovery_events = [
            event for event in events if event["event_type"] == "RECOVERY_FENCE_COMMITTED"
        ]
        assert len(recovery_events) == 1
        event = recovery_events[0]
        assert event["event_id"] == response.decision_id
        assert event["evidence_digest"] == hashlib.sha256(_SECRET).hexdigest()
        assert event["fencing_epoch"] == 1
        assert event["token_id"]
        assert "attestation" not in event
        assert _SECRET.decode("utf-8", errors="ignore") not in json.dumps(event)


@pytest.mark.docker
async def test_jetstream_outage_keeps_durable_outbox_then_republishes_once(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    trusted_nats_container,
) -> None:
    authority = authority_factory()
    nats_url, container = trusted_nats_container
    subprocess.run(["docker", "start", container], capture_output=True, check=True, timeout=20)
    await _wait_nats_ready(nats_url)
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority), nats_url=nats_url
    ) as context:
        await _wait_audit_drained(context.stack.db_path)
        subprocess.run(["docker", "stop", container], capture_output=True, check=True, timeout=20)
        _wait_port(int(nats_url.rsplit(":", 1)[1]), open_expected=False)
        audit = vars(context.service)["_audit"]
        assert audit is not None and audit.operational is True
        try:
            response = await _recover(context, pki)
            assert response.state == "UNBLOCKED"
            assert context.service.executor.blocked_devices() == frozenset()
            assert _pending_audit_counts(context.stack.db_path)[1] == 1
            assert _decision_count(context.stack.db_path) == 1
        finally:
            subprocess.run(
                ["docker", "start", container], capture_output=True, check=True, timeout=20
            )
            await _wait_nats_ready(nats_url)
        await _wait_audit_drained(context.stack.db_path)
        connection = await _connect(nats_url)
        try:
            payloads = await _fetch_all(
                connection.jetstream(timeout=5), context.stack.stream, context.stack.subject, 3
            )
        finally:
            await connection.drain()
        events = [json.loads(payload) for payload in payloads]
        assert [event["event_type"] for event in events] == [
            "ACCEPTED",
            "COMPLETED",
            "RECOVERY_FENCE_COMMITTED",
        ]
        assert len({event["event_id"] for event in events}) == 3
        await _assert_unknown(context)
        assert modbus_server.write_count == 0


async def test_process_crash_after_commit_requires_new_fencing_and_never_replays(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory()
    parent, child = multiprocessing.get_context("spawn").Pipe()
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_commit_process,
        args=(child, tmp_path / "crash-child", pki, modbus_server.port, _settings(pki, authority)),
        name="commander-commit-crash",
    )
    process.start()
    child.close()
    try:
        ready = await _receive_process_message(parent)
        channel = grpc.aio.secure_channel(
            ready["target"],
            grpc.ssl_channel_credentials(
                root_certificates=pki.ca_cert,
                private_key=pki.key("ops"),
                certificate_chain=pki.cert("ops"),
            ),
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )
        try:
            with pytest.raises(grpc.aio.AioRpcError):
                await commander_pb2_grpc.CommanderServiceStub(channel).RecoverDeviceControl(
                    pb.RecoverDeviceControlRequest(
                        device_id="WT01",
                        expected_revision=ready["revision"],
                        request_id="req-crash-after-commit",
                        isolation_attestation=_SECRET,
                    ),
                    timeout=8,
                )
        finally:
            await channel.close()
        committed = await _receive_process_message(parent)
        assert committed["event"] == "committed"
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == 73
        database_path = Path(ready["db_path"])
        assert _decision_count(database_path) == 1

        async with _recovery_context(
            tmp_path / "after-crash",
            pki,
            modbus_server,
            _settings(pki, authority),
            database_source=database_path,
        ) as restarted:
            old = await restarted.service.control.get_command(ready["command_id"])
            assert old is not None and old.outcome is not None
            assert old.state.value == old.outcome.value == "UNKNOWN"
            assert modbus_server.write_count == 0
            response = await _recover(restarted, pki, request_id="req-after-crash")
            assert response.state == "UNBLOCKED"
            await _submit_and_wait(restarted, pki, "req-new-command-after-crash")
            old_again = await restarted.service.control.get_command(ready["command_id"])
            assert old_again is not None and old_again.outcome is not None
            assert old_again.state.value == old_again.outcome.value == "UNKNOWN"
            assert modbus_server.write_count == 1
    finally:
        parent.close()
        if process.is_alive():
            process.kill()
        process.join(timeout=5)
        process.close()


@pytest.mark.docker
async def test_recovery_and_graceful_shutdown_are_serialized(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
    trusted_nats_container,
) -> None:
    authority = authority_factory({"delay_seconds": 0.5})
    nats_url, _container = trusted_nats_container
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority), nats_url=nats_url
    ) as context:
        recovery = asyncio.create_task(_recover(context, pki))
        async with asyncio.timeout(5):
            while not authority.state()["requests"]:
                await asyncio.sleep(0.02)
        stopping = asyncio.create_task(context.service.stop())
        response = await recovery
        stopped = await stopping
        assert response.state == "UNBLOCKED"
        assert stopped == frozenset()
        health = context.service.health()
        assert health.state is ServiceState.STOPPED
        assert health.ready is False
        assert health.pending_commands == 0
        assert health.inflight_io == 0
        assert health.blocked_devices == ()
        _assert_unknown_durable(context.stack.db_path, context.command_ids)
        assert modbus_server.write_count == 0


async def test_recovery_and_reload_are_serialized(
    tmp_path: Path,
    pki: Pki,
    modbus_server: Any,
    authority_factory: Callable[..., FencingAuthorityProcess],
) -> None:
    authority = authority_factory({"delay_seconds": 0.5})
    async with _recovery_context(
        tmp_path, pki, modbus_server, _settings(pki, authority)
    ) as context:
        recovery = asyncio.create_task(_recover(context, pki))
        async with asyncio.timeout(5):
            while not authority.state()["requests"]:
                await asyncio.sleep(0.02)
        reloading = asyncio.create_task(context.service.reload())
        await asyncio.sleep(0.1)
        assert not reloading.done()
        response = await recovery
        await reloading
        assert response.state == "UNBLOCKED"
        assert context.service.executor.blocked_devices() == frozenset()
        await _assert_unknown(context)
        assert modbus_server.write_count == 0
