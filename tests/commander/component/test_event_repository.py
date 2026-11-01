"""EventRepository 组件测试：真实 SQLite（WAL/FULL），不模拟数据库。"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from commander.application.control import CommandControl
from commander.domain.command import (
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
    CommandStateError,
)
from commander.infrastructure.event_repository import (
    EventRepository,
    RepositoryError,
    StorageFullError,
)
from core.domain.sample_value import WritableScalar

pytestmark = pytest.mark.component


def _spec(
    device: str = "WT01", point: str = "setpoint", value: WritableScalar = 12.5
) -> CommandSpec:
    return CommandSpec(device, point, value, "ref-abc")


def _must(result: CommandResult | None) -> CommandResult:
    assert result is not None
    return result


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[EventRepository]:
    repository = EventRepository(tmp_path / "commander.db", retention_seconds=1800)
    yield repository
    repository.close()


def test_accept_persists_batch_with_shared_request_and_ordered_seq(repo: EventRepository) -> None:
    accepted = repo.accept(
        [_spec(point="a"), _spec(point="b")],
        caller_id="operator-1",
        operator_id="human",
        trace_id="trace-1",
    )
    assert len(accepted) == 2
    assert accepted[0].request_id == accepted[1].request_id
    assert accepted[0].command_id != accepted[1].command_id
    assert accepted[0].receive_seq == 1
    assert accepted[1].receive_seq == 2
    results = repo.get_request(accepted[0].request_id)
    assert [result.command.spec.point_id for result in results] == ["a", "b"]
    got = _must(repo.get(accepted[0].command_id))
    assert got is not None
    assert got.state is CommandState.ACCEPTED
    assert got.command.caller_id == "operator-1"
    assert got.completed_at is None


def test_rejects_empty_batch_and_blank_caller(repo: EventRepository) -> None:
    with pytest.raises(ValueError):
        repo.accept([], caller_id="x")
    with pytest.raises(ValueError):
        repo.accept([_spec()], caller_id=" ")


def test_full_lifecycle_events_and_terminal_once(repo: EventRepository) -> None:
    command = repo.accept([_spec()], caller_id="op")[0]
    repo.transition(command.command_id, CommandState.CONNECTING)
    repo.transition(command.command_id, CommandState.EXECUTING)
    repo.transition(
        command.command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )
    result = _must(repo.get(command.command_id))
    assert result is not None
    assert result.state is CommandState.SUCCEEDED
    assert result.outcome is CommandOutcome.CONFIRMED_SUCCESS
    assert result.completed_at is not None
    with pytest.raises(CommandStateError):
        repo.transition(
            command.command_id,
            CommandState.FAILED,
            outcome=CommandOutcome.CONFIRMED_FAILURE,
        )


def test_illegal_transition_rolls_back_and_keeps_repository_usable(repo: EventRepository) -> None:
    command = repo.accept([_spec()], caller_id="op")[0]
    with pytest.raises(CommandStateError):
        repo.transition(
            command.command_id,
            CommandState.SUCCEEDED,  # ACCEPTED -> SUCCEEDED 非法
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )
    # 领域校验发生在 INSERT 之前；非法转移后仓储继续可用。
    assert repo.operational
    repo.transition(command.command_id, CommandState.CONNECTING)
    assert _must(repo.get(command.command_id)).state is CommandState.CONNECTING


def test_recover_after_executing_is_unknown_never_not_sent(repo: EventRepository) -> None:
    # T2 已落盘：崩溃恢复必须 UNKNOWN，绝不假定未发送。
    t2 = repo.accept([_spec()], caller_id="op")[0]
    repo.transition(t2.command_id, CommandState.CONNECTING)
    repo.transition(t2.command_id, CommandState.EXECUTING)
    # 更早阶段崩溃：ACCEPTED / CONNECTING -> INTERRUPTED / NOT_SENT。
    accepted_only = repo.accept([_spec(point="b")], caller_id="op")[0]
    connecting = repo.accept([_spec(point="c")], caller_id="op")[0]
    repo.transition(connecting.command_id, CommandState.CONNECTING)
    finished = repo.accept([_spec(point="d")], caller_id="op")[0]
    repo.transition(finished.command_id, CommandState.CONNECTING)
    repo.transition(finished.command_id, CommandState.EXECUTING)
    repo.transition(
        finished.command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )

    recovered = repo.recover()
    assert set(recovered) == {
        t2.command_id,
        accepted_only.command_id,
        connecting.command_id,
    }

    t2_result = _must(repo.get(t2.command_id))
    assert t2_result.state is CommandState.UNKNOWN
    assert t2_result.outcome is CommandOutcome.UNKNOWN
    early = _must(repo.get(accepted_only.command_id))
    assert early.state is CommandState.INTERRUPTED
    assert early.outcome is CommandOutcome.NOT_SENT
    mid = _must(repo.get(connecting.command_id))
    assert mid.state is CommandState.INTERRUPTED
    assert mid.outcome is CommandOutcome.NOT_SENT
    # 恢复幂等：第二次没有待决命令。
    assert repo.recover() == ()
    done = _must(repo.get(finished.command_id))
    assert done.state is CommandState.SUCCEEDED


def test_recovery_survives_process_restart_with_real_reopen(tmp_path: Path) -> None:
    db = tmp_path / "commander.db"
    first = EventRepository(db)
    command = first.accept([_spec()], caller_id="op")[0]
    first.transition(command.command_id, CommandState.CONNECTING)
    first.transition(command.command_id, CommandState.EXECUTING)
    first.close()

    second = EventRepository(db)
    try:
        recovered = second.recover()
        assert recovered == (command.command_id,)
        result = _must(second.get(command.command_id))
        assert result.state is CommandState.UNKNOWN
        assert result.outcome is CommandOutcome.UNKNOWN
    finally:
        second.close()


def test_idempotency_replay_returns_same_commands(repo: EventRepository) -> None:
    specs = [_spec()]
    first = repo.accept(specs, caller_id="op", idempotency_key="key-1")
    second = repo.accept(specs, caller_id="op", idempotency_key="key-1")
    assert tuple(c.command_id for c in first) == tuple(c.command_id for c in second)
    assert tuple(c.command_id for c in repo.find_idempotent("op", "key-1")) == tuple(
        c.command_id for c in first
    )
    # 同一 key 不同批量签名必须拒绝。
    with pytest.raises(ValueError):
        repo.accept([_spec(value=99.0)], caller_id="op", idempotency_key="key-1")
    # key 按调用方隔离。
    other = repo.accept(specs, caller_id="someone-else", idempotency_key="key-1")
    assert other[0].command_id != first[0].command_id


def test_outbox_pending_mark_and_failure_attempts(repo: EventRepository) -> None:
    command = repo.accept([_spec()], caller_id="op")[0]
    repo.transition(command.command_id, CommandState.CONNECTING)
    pending = repo.pending_events(100)
    assert [event["event_type"] for event in pending] == ["ACCEPTED", "CONNECTING"]
    assert all(event["publish_state"] == "PENDING" for event in pending)
    accepted_event = pending[0]
    assert accepted_event["event_id"]
    assert "publish_state" in accepted_event  # 发箱前由 publisher 剥离
    repo.record_publish_failure(accepted_event["event_id"])
    repo.record_publish_failure(accepted_event["event_id"])
    still_pending = repo.pending_events(100)
    assert still_pending[0]["attempts"] == 2
    repo.mark_published(accepted_event["event_id"])
    remaining = repo.pending_events(100)
    assert [event["event_id"] for event in remaining] == [pending[1]["event_id"]]
    # 幂等标记。
    repo.mark_published(accepted_event["event_id"])


def test_cleanup_keeps_unpublished_or_unfinished_requests(tmp_path: Path) -> None:
    repo = EventRepository(tmp_path / "c.db", retention_seconds=0)
    try:
        old = repo.accept([_spec(point="old")], caller_id="op")[0]
        repo.transition(old.command_id, CommandState.CONNECTING)
        repo.transition(old.command_id, CommandState.EXECUTING)
        repo.transition(
            old.command_id,
            CommandState.SUCCEEDED,
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )
        # 终态但审计未发布：不得清理。
        unpublished = repo.accept([_spec(point="unpub")], caller_id="op")[0]
        repo.transition(unpublished.command_id, CommandState.CONNECTING)
        repo.transition(unpublished.command_id, CommandState.EXECUTING)
        repo.transition(
            unpublished.command_id,
            CommandState.FAILED,
            outcome=CommandOutcome.CONFIRMED_FAILURE,
        )
        # 未完成：不得清理。
        pending = repo.accept([_spec(point="pending")], caller_id="op")[0]
        time.sleep(0.01)
        for event in repo.pending_events(100):
            if event["command_id"] == old.command_id:
                repo.mark_published(event["event_id"])
        # old 请求所有事件（含 COMPLETED）都已发布。
        deleted = repo.cleanup()
        assert deleted == 1
        assert repo.get(old.command_id) is None
        assert repo.get(unpublished.command_id) is not None
        assert repo.get(pending.command_id) is not None
    finally:
        repo.close()


def test_admission_stops_before_reserved_capacity(tmp_path: Path) -> None:
    # 极小配额：预留空间必须阻止新 ACCEPTED，保留终态事件能力。
    repo = EventRepository(
        tmp_path / "small.db",
        max_storage_bytes=64 * 1024,
        reserved_storage_bytes=32 * 1024,
        admission_stop_ratio=0.9,
    )
    try:
        with pytest.raises(StorageFullError):
            repo.accept(
                [CommandSpec("WT01", "p", "x" * 4000, "ref")],
                caller_id="op",
            )
        assert repo.operational
    finally:
        repo.close()


def test_flock_rejects_second_process_owner(tmp_path: Path) -> None:
    db = tmp_path / "locked.db"
    first = EventRepository(db)
    try:
        with pytest.raises(RepositoryError, match="owned by another process"):
            EventRepository(db)
    finally:
        first.close()


def test_pragmas_and_schema_on_real_database_file(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    repository = EventRepository(db)
    try:
        raw = sqlite3.connect(db)
        try:
            assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert raw.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            names = {
                row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {"command_events", "idempotency_keys", "repository_meta"} <= names
        finally:
            raw.close()
    finally:
        repository.close()


def test_metrics_report_accepted_and_terminal_counts(repo: EventRepository) -> None:
    first = repo.accept([_spec()], caller_id="op")[0]
    repo.transition(first.command_id, CommandState.CONNECTING)
    repo.transition(first.command_id, CommandState.EXECUTING)
    repo.transition(
        first.command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )
    repo.accept([_spec(point="b")], caller_id="op")
    metrics = repo.metrics()
    assert metrics.accepted_total == 2
    assert dict(metrics.terminal_counts).get("SUCCEEDED") == 1
    assert metrics.pending_events >= 3
    assert metrics.used_bytes > 0


def test_command_control_maps_service_boundaries(repo: EventRepository) -> None:
    control = CommandControl(repo)
    command = control.accept([_spec()], caller_id="op")[0]
    control.connecting(command.command_id)
    control.executing(command.command_id)
    control.complete(
        command.command_id,
        CommandState.UNKNOWN,
        outcome=CommandOutcome.UNKNOWN,
        error_code="WRITE_TIMEOUT",
    )
    result = _must(control.get_command(command.command_id))
    assert result.state is CommandState.UNKNOWN
    assert result.error_code == "WRITE_TIMEOUT"
