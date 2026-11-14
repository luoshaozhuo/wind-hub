"""RecoverDeviceControl 的真实 mTLS 边界与 fail-closed 行为。

这些测试只验证尚未配置独立可信证明方时的拒绝路径；不把任意字节、
认证成功或测试替身解释为现场隔离证据，也不宣称恢复成功能力已实现。
"""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import grpc
import pytest

from commander.application.persistence import BufferedJournal
from commander.application.service_lifecycle import ServiceState
from commander.domain.command import CommandSpec
from commander.infrastructure.event_repository import EventRepository
from contracts import commander_pb2 as pb
from tests.commander.grpc_harness import GrpcStack, Pki, build_stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.real_service,
]

_SECRET = b"untrusted-sensitive-attestation-\x00\xff"


def _request(**changes: object) -> pb.RecoverDeviceControlRequest:
    values: dict[str, object] = {
        "device_id": "WT01",
        "expected_revision": "revision-1",
        "request_id": "req-recovery-1",
        "isolation_attestation": _SECRET,
    }
    values.update(changes)
    return pb.RecoverDeviceControlRequest(**values)  # type: ignore[arg-type]


async def _aborts(call: Awaitable[Any], code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await call
    assert exc_info.value.code() is code, exc_info.value
    return exc_info.value


async def test_recover_scope_excludes_viewer_and_allows_only_ops_deployer_to_boundary(
    pki: Pki, grpc_stack: GrpcStack
) -> None:
    for client in ("viewer", "stranger"):
        _common, commander = grpc_stack.stubs(pki, client=client)
        await _aborts(
            commander.RecoverDeviceControl(_request(), timeout=5),
            grpc.StatusCode.PERMISSION_DENIED,
        )
    for client in ("ops", "deployer"):
        _common, commander = grpc_stack.stubs(pki, client=client)
        error = await _aborts(
            commander.RecoverDeviceControl(_request(), timeout=5),
            grpc.StatusCode.FAILED_PRECONDITION,
        )
        assert error.details() == "trusted recovery verification or commit failed"
    _common, commander = grpc_stack.stubs(pki, client="ops")
    await _aborts(
        commander.RecoverDeviceControl(_request(isolation_attestation=b"x" * 65536), timeout=5),
        grpc.StatusCode.FAILED_PRECONDITION,
    )


@pytest.mark.parametrize(
    "recovery_request",
    [
        _request(device_id=""),
        _request(device_id="   "),
        _request(expected_revision=""),
        _request(request_id=""),
        _request(request_id="x" * 129),
        _request(request_id="证" * 43),
        _request(isolation_attestation=b""),
        _request(isolation_attestation=b"x" * 65537),
    ],
    ids=(
        "empty-device",
        "blank-device",
        "empty-revision",
        "empty-request-id",
        "request-id-129-bytes",
        "request-id-multibyte-129-bytes",
        "empty-attestation",
        "attestation-65537-bytes",
    ),
)
async def test_invalid_recovery_request_is_rejected_without_echoing_evidence(
    pki: Pki, grpc_stack: GrpcStack, recovery_request: pb.RecoverDeviceControlRequest
) -> None:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    error = await _aborts(
        commander.RecoverDeviceControl(recovery_request, timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    assert _SECRET.decode("utf-8", errors="ignore") not in error.details()


async def test_unsigned_expired_or_replayed_attestation_never_unblocks_recovered_device(
    pki: Pki, tmp_path: Path, modbus_server: object, caplog: pytest.LogCaptureFixture
) -> None:
    """真实 SQLite 恢复 UNKNOWN 后，伪造/过期语义/重放材料均保持 BLOCKED。"""
    command_id = ""

    def seed_unfinished(service_dir: Path) -> None:
        nonlocal command_id
        repository = EventRepository(service_dir / "state" / "commander.db")
        journal = BufferedJournal()
        accepted = journal.accept(
            [CommandSpec("WT01", "setpoint_power", 10.0, "recovered-revision")],
            request_id="req-before-recovery-rpc",
            caller_id="ops-console",
        )
        command_id = accepted[0].command_id
        repository.append_events(journal.snapshot(100))
        repository.close()

    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,  # type: ignore[attr-defined]
        prepare=seed_unfinished,
        label="recovery-rpc",
    )
    try:
        before = await service.control.get_command(command_id)
        assert before is not None and before.outcome is not None
        assert before.state.value == before.outcome.value == "UNKNOWN"
        assert service.health().state is ServiceState.DEGRADED
        assert service.executor.blocked_devices() == frozenset({"WT01"})
        retained_before = dict(service.executor._retained_permits)  # noqa: SLF001
        settled_before = set(service.executor._settled_unknown)  # noqa: SLF001

        _common, commander = stack.stubs(pki, client="ops")
        for evidence in (
            _SECRET,
            b'{"issuer":"forged","expires_at":"2000-01-01T00:00:00Z"}',
            _SECRET,
        ):
            error = await _aborts(
                commander.RecoverDeviceControl(_request(isolation_attestation=evidence), timeout=5),
                grpc.StatusCode.FAILED_PRECONDITION,
            )
            assert _SECRET.decode("utf-8", errors="ignore") not in error.details()
        assert _SECRET.decode("utf-8", errors="ignore") not in caplog.text

        after = await service.control.get_command(command_id)
        assert after is not None and after.outcome is not None
        assert after.state.value == after.outcome.value == "UNKNOWN"
        assert service.executor.blocked_devices() == frozenset({"WT01"})
        assert service.health().state is ServiceState.DEGRADED
        assert dict(service.executor._retained_permits) == retained_before  # noqa: SLF001
        assert set(service.executor._settled_unknown) == settled_before  # noqa: SLF001
        assert modbus_server.write_count == 0  # type: ignore[attr-defined]
    finally:
        await server.stop(grace=5)
        assert await service.stop() == frozenset({"WT01"})


def test_shipped_roles_reserve_recover_for_maintenance() -> None:
    """出厂仅 maintenance 可恢复；普通 EMS 控制身份不得隐式获得该权限。"""
    import yaml

    shipped = Path(__file__).resolve().parents[3] / "configs" / "services" / "commander.yaml"
    document = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    callers: list[dict[str, object]] = document["security"]["callers"]
    grants: dict[object, set[object]] = {
        caller["caller_id"]: set(caller["permissions"])
        for caller in callers  # type: ignore[index]
    }
    assert "recover" not in grants["ems"]
    assert "recover" in grants["maintenance"]
    assert "viewer" not in grants
