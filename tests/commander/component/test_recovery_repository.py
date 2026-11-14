"""恢复阻断、代次、防重放、原子 Outbox 与保留期的真实 SQLite 测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from commander.domain.command import CommandOutcome, CommandSpec, CommandState
from commander.infrastructure.event_repository import EventRepository, RepositoryError

from ..seed_events import seed_accept, seed_transition

pytestmark = pytest.mark.component


def _commit(
    repository: EventRepository,
    *,
    request_id: str,
    token_id: str,
    epoch: int,
    device_id: str = "WT01",
    digest: str = "a" * 64,
) -> str:
    return repository.commit_recovery(
        caller_id="ops-console",
        request_id=request_id,
        device_id=device_id,
        revision="revision-1",
        evidence_digest=digest,
        token_id=token_id,
        fencing_epoch=epoch,
        old_instance="old-instance",
        new_instance="new-instance",
    )


def test_recovery_block_survives_unknown_restart_cleanup_and_successful_decision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovery.db"
    first = EventRepository(path, retention_seconds=0)
    command = seed_accept(
        first,
        [CommandSpec("WT01", "setpoint", 1.0, "revision-1")],
        request_id="req-unknown",
        caller_id="ops",
    )[0]
    seed_transition(first, command.command_id, CommandState.EXECUTING)
    first.close()

    second = EventRepository(path, retention_seconds=0)
    try:
        assert second.recover() == (command.command_id,)
        assert second.recovery_blocked_devices() == frozenset({"WT01"})
        assert second.recover() == ()
        for event in second.pending_events(100):
            second.mark_published(str(event["event_id"]))
        assert second.cleanup() == 1
        assert second.get(command.command_id) is None
        assert second.recovery_blocked_devices() == frozenset({"WT01"})
        _commit(second, request_id="req-recover", token_id="token-1", epoch=1)
        # 成功裁决只授权当前进程解除内存阻断，不能删除下次启动的安全屏障。
        assert second.recovery_blocked_devices() == frozenset({"WT01"})
    finally:
        second.close()

    third = EventRepository(path, retention_seconds=0)
    try:
        assert third.recover() == ()
        assert third.recovery_blocked_devices() == frozenset({"WT01"})
    finally:
        third.close()


def test_upgrade_rebuilds_blocks_from_unknown_before_history_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    repository = EventRepository(path)
    command = seed_accept(
        repository,
        [CommandSpec("WT01", "setpoint", 1.0, "revision-1")],
        caller_id="ops",
    )[0]
    repository.recover()
    repository.close()
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TABLE recovery_blocks")
        raw.commit()
    finally:
        raw.close()

    upgraded = EventRepository(path)
    try:
        assert upgraded.recovery_blocked_devices() == frozenset({"WT01"})
        result = upgraded.get(command.command_id)
        assert result is not None and result.state is CommandState.UNKNOWN
        assert result.outcome is CommandOutcome.UNKNOWN
    finally:
        upgraded.close()


def test_token_uniqueness_and_epoch_monotonicity_are_enforced(tmp_path: Path) -> None:
    repository = EventRepository(tmp_path / "tokens.db")
    try:
        _commit(repository, request_id="req-a", token_id="token-a", epoch=1)
        with pytest.raises(FileExistsError):
            _commit(repository, request_id="req-b", token_id="token-a", epoch=2)
        with pytest.raises(ValueError):
            _commit(repository, request_id="req-c", token_id="token-c", epoch=1)
        _commit(repository, request_id="req-d", token_id="token-d", epoch=2)
        assert _commit(repository, request_id="req-d", token_id="token-d", epoch=2)
        with pytest.raises(FileExistsError):
            _commit(repository, request_id="req-d", token_id="token-d", epoch=2, digest="b" * 64)
    finally:
        repository.close()


def test_idempotent_old_epoch_cannot_authorize_a_new_release(tmp_path: Path) -> None:
    repository = EventRepository(tmp_path / "epochs.db")
    try:
        first = _commit(repository, request_id="req-old", token_id="token-old", epoch=1)
        _commit(repository, request_id="req-new", token_id="token-new", epoch=2)
        # 已存在更新的设备代次时，旧请求的幂等查询身份不能再次成为解除依据。
        with pytest.raises(ValueError):
            _commit(repository, request_id="req-old", token_id="token-old", epoch=1)
        assert repository.recovery_decision("ops-console", "req-old") is not None
        assert first
    finally:
        repository.close()


def test_recovery_outbox_has_reserved_slot_metrics_and_no_raw_attestation(
    tmp_path: Path,
) -> None:
    repository = EventRepository(tmp_path / "outbox.db")
    try:
        for index in range(250):
            seed_accept(
                repository,
                [CommandSpec("WT01", f"p-{index}", 1.0, "revision-1")],
                request_id=f"req-command-{index}",
                caller_id="ops",
            )
        decision_id = _commit(
            repository,
            request_id="req-recovery",
            token_id="token-recovery",
            epoch=1,
        )
        batch = repository.pending_events(100)
        assert len(batch) == 100
        recovery = [event for event in batch if event["event_type"] == "RECOVERY_FENCE_COMMITTED"]
        assert len(recovery) == 1
        assert recovery[0]["event_id"] == decision_id
        assert "attestation" not in recovery[0]
        assert recovery[0]["evidence_digest"] == "a" * 64
        assert repository.metrics().pending_events == 251
    finally:
        repository.close()


def test_sqlite_insert_failure_rolls_back_decision_and_outbox(tmp_path: Path) -> None:
    path = tmp_path / "rollback.db"
    repository = EventRepository(path)
    try:
        raw = sqlite3.connect(path)
        try:
            raw.executescript(
                "CREATE TRIGGER fail_recovery_insert BEFORE INSERT ON recovery_decisions "
                "BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END;"
            )
        finally:
            raw.close()
        with pytest.raises(RepositoryError, match="SQLite transaction failed"):
            _commit(repository, request_id="req-fail", token_id="token-fail", epoch=1)
        assert repository.recovery_decision("ops-console", "req-fail") is None
        assert repository.pending_events() == ()
        assert repository.operational
    finally:
        repository.close()
