"""EventRepository 组件测试：真实 SQLite（WAL/FULL），不模拟数据库。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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


def test_each_submission_creates_fresh_request_per_rr02(repo: EventRepository) -> None:
    """RR-02：重复提交是新请求；服务端不接受、不依赖调用方幂等编号。"""
    specs = [_spec()]
    first = repo.accept(specs, caller_id="op")
    second = repo.accept(specs, caller_id="op")
    # 新 request_id、新 command_id、receive_seq 全局递增。
    assert first[0].request_id != second[0].request_id
    assert first[0].command_id != second[0].command_id
    assert first[0].receive_seq == 1
    assert second[0].receive_seq == 2
    # 两个请求可分别按 request_id 查询。
    first_members = repo.get_request(first[0].request_id)
    second_members = repo.get_request(second[0].request_id)
    assert [c.command.command_id for c in first_members] == [first[0].command_id]
    assert [c.command.command_id for c in second_members] == [second[0].command_id]
    # 仓储接口不再暴露任何调用方幂等键能力。
    assert not hasattr(repo, "find_idempotent")
    import inspect

    assert "idempotency_key" not in inspect.signature(repo.accept).parameters


def test_legacy_database_with_idempotency_table_still_usable(tmp_path: Path) -> None:
    """旧版本残留 idempotency_keys 表：升级打开不破坏既有命令记录。"""
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    try:
        legacy.execute(
            "CREATE TABLE idempotency_keys ("
            "caller_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,"
            "request_id TEXT NOT NULL, command_count INTEGER NOT NULL,"
            "created_at TEXT NOT NULL, PRIMARY KEY(caller_id, idempotency_key))"
        )
        legacy.execute(
            "INSERT INTO idempotency_keys VALUES ('op','old-key','req-legacy',1,'2026-01-01')"
        )
        legacy.commit()
    finally:
        legacy.close()

    upgraded = EventRepository(db)
    try:
        accepted = upgraded.accept([_spec()], caller_id="op")
        upgraded.transition(accepted[0].command_id, CommandState.CONNECTING)
        upgraded.transition(accepted[0].command_id, CommandState.EXECUTING)
        upgraded.transition(
            accepted[0].command_id,
            CommandState.SUCCEEDED,
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )
        result = _must(upgraded.get(accepted[0].command_id))
        assert result.state is CommandState.SUCCEEDED
        raw = sqlite3.connect(db)
        try:
            names = {
                row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # 旧表保留但不属于新 schema 契约；新表与旧记录共存可读。
            assert {"command_events", "repository_meta"} <= names
            leftover = raw.execute(
                "SELECT request_id FROM idempotency_keys WHERE idempotency_key='old-key'"
            ).fetchone()
            assert leftover == ("req-legacy",)
        finally:
            raw.close()
    finally:
        upgraded.close()


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
            assert {"command_events", "repository_meta"} <= names
            assert "idempotency_keys" not in names
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


async def test_command_control_maps_service_boundaries(
    tmp_path: Path, sqlite_writer: ThreadPoolExecutor
) -> None:
    repo = sqlite_writer.submit(
        lambda: EventRepository(tmp_path / "ctrl.db", retention_seconds=1800)
    ).result()
    try:
        control = CommandControl(repo, sqlite_writer)
        command = (await control.accept([_spec()], caller_id="op"))[0]
        await control.connecting(command.command_id)
        await control.executing(command.command_id)
        await control.complete(
            command.command_id,
            CommandState.UNKNOWN,
            outcome=CommandOutcome.UNKNOWN,
            error_code="WRITE_TIMEOUT",
        )
        result = _must(await control.get_command(command.command_id))
        assert result.state is CommandState.UNKNOWN
        assert result.error_code == "WRITE_TIMEOUT"
    finally:
        sqlite_writer.submit(repo.close).result()


async def test_control_cancellation_waits_for_durable_t1_settlement(
    tmp_path: Path, sqlite_writer: ThreadPoolExecutor
) -> None:
    """T1 提交在 writer 队列中等待时调用方被取消：提交不得丢失。

    单线程池先被阻塞任务占住，accept 只能排队；此时取消调用方协程，
    CommandControl 必须等待已提交的 SQLite 工作落定后再传播取消，
    禁止出现“SQLite 提交成功但调用链无人知晓”的孤儿 T1。
    """
    import threading

    repo = sqlite_writer.submit(
        lambda: EventRepository(tmp_path / "cancel.db", retention_seconds=1800)
    ).result()
    try:
        release = threading.Event()
        sqlite_writer.submit(lambda: release.wait(5.0))  # 占住唯一 writer 线程
        control = CommandControl(repo, sqlite_writer)
        task = asyncio.create_task(control.accept([_spec()], caller_id="op"))
        await asyncio.sleep(0.05)  # 确保 accept 已进入 writer 队列但无法执行
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        # 被屏蔽的提交最终必须落盘可查（经 writer 线程读取，避免跨线程）。
        for _ in range(200):
            total = sqlite_writer.submit(repo.metrics).result().accepted_total
            if total == 1:
                break
            await asyncio.sleep(0.01)
        assert sqlite_writer.submit(repo.metrics).result().accepted_total == 1
    finally:
        sqlite_writer.submit(repo.close).result()
