"""EventRepository 组件测试：真实 SQLite（WAL/FULL），不模拟数据库。"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from commander.application.control import CommandControl
from commander.application.persistence import BufferedJournal
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
)
from core.domain.sample_value import WritableScalar

from ..seed_events import seed_accept, seed_transition

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
    accepted = seed_accept(
        repo,
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
        seed_accept(repo, [], caller_id="x")
    with pytest.raises(ValueError):
        seed_accept(repo, [_spec()], caller_id=" ")


def test_full_lifecycle_events_and_terminal_once(repo: EventRepository) -> None:
    command = seed_accept(repo, [_spec()], caller_id="op")[0]
    seed_transition(repo, command.command_id, CommandState.CONNECTING)
    seed_transition(repo, command.command_id, CommandState.EXECUTING)
    seed_transition(
        repo,
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
        seed_transition(
            repo,
            command.command_id,
            CommandState.FAILED,
            outcome=CommandOutcome.CONFIRMED_FAILURE,
        )


def test_illegal_transition_rolls_back_and_keeps_repository_usable(repo: EventRepository) -> None:
    command = seed_accept(repo, [_spec()], caller_id="op")[0]
    with pytest.raises(CommandStateError):
        seed_transition(
            repo,
            command.command_id,
            CommandState.SUCCEEDED,  # ACCEPTED -> SUCCEEDED 非法
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )
    # 领域校验发生在 INSERT 之前；非法转移后仓储继续可用。
    assert repo.operational
    seed_transition(repo, command.command_id, CommandState.CONNECTING)
    assert _must(repo.get(command.command_id)).state is CommandState.CONNECTING


def test_recover_after_executing_is_unknown_never_not_sent(repo: EventRepository) -> None:
    # T2 已落盘：崩溃恢复必须 UNKNOWN，绝不假定未发送。
    t2 = seed_accept(repo, [_spec()], caller_id="op")[0]
    seed_transition(repo, t2.command_id, CommandState.CONNECTING)
    seed_transition(repo, t2.command_id, CommandState.EXECUTING)
    # 更早阶段崩溃：ACCEPTED / CONNECTING -> INTERRUPTED / NOT_SENT。
    accepted_only = seed_accept(repo, [_spec(point="b")], caller_id="op")[0]
    connecting = seed_accept(repo, [_spec(point="c")], caller_id="op")[0]
    seed_transition(repo, connecting.command_id, CommandState.CONNECTING)
    finished = seed_accept(repo, [_spec(point="d")], caller_id="op")[0]
    seed_transition(repo, finished.command_id, CommandState.CONNECTING)
    seed_transition(repo, finished.command_id, CommandState.EXECUTING)
    seed_transition(
        repo,
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
    command = seed_accept(first, [_spec()], caller_id="op")[0]
    seed_transition(first, command.command_id, CommandState.CONNECTING)
    seed_transition(first, command.command_id, CommandState.EXECUTING)
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
    first = seed_accept(repo, specs, caller_id="op")
    second = seed_accept(repo, specs, caller_id="op")
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

    assert "idempotency_key" not in inspect.signature(BufferedJournal.accept).parameters


def _append_event(
    *,
    event_id: str,
    command_id: str,
    receive_seq: int,
    event_type: str = "ACCEPTED",
    state: str = "ACCEPTED",
) -> dict[str, object]:
    fields: dict[str, object] = {
        "event_id": event_id,
        "command_id": command_id,
        "event_type": event_type,
        "occurred_at": "2026-11-01T00:00:00+00:00",
        "state": state,
    }
    if event_type == "ACCEPTED":
        fields.update(
            request_id=f"req-{command_id}",
            caller_id="op",
            device_id="WT01",
            point_id="p",
            requested_value="1.0",
            config_reference="ref",
            receive_seq=receive_seq,
        )
    return fields


def test_append_events_upserts_receive_seq_on_fresh_database(tmp_path: Path) -> None:
    """write-behind 首写全新库：receive_seq 行必须被创建，重启后序号不得归零。"""
    db = tmp_path / "wb.db"
    first = EventRepository(db)
    try:
        first.append_events(
            [
                _append_event(event_id="e1", command_id="c1", receive_seq=1),
                _append_event(event_id="e3", command_id="c3", receive_seq=3),
            ]
        )
        assert first.latest_receive_seq() == 3
    finally:
        first.close()

    second = EventRepository(db)
    try:
        # 重开库（模拟进程重启）：BufferedJournal 的 initial_sequence 必须从 3 续接。
        assert second.latest_receive_seq() == 3
        # worker 重试/乱序批次不得让高水位回退。
        second.append_events([_append_event(event_id="e2", command_id="c2", receive_seq=2)])
        assert second.latest_receive_seq() == 3
        second.append_events([_append_event(event_id="e4", command_id="c4", receive_seq=4)])
        assert second.latest_receive_seq() == 4
    finally:
        second.close()


def test_outbox_pending_mark_and_failure_attempts(repo: EventRepository) -> None:
    command = seed_accept(repo, [_spec()], caller_id="op")[0]
    seed_transition(repo, command.command_id, CommandState.CONNECTING)
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
        old = seed_accept(repo, [_spec(point="old")], caller_id="op")[0]
        seed_transition(repo, old.command_id, CommandState.CONNECTING)
        seed_transition(repo, old.command_id, CommandState.EXECUTING)
        seed_transition(
            repo,
            old.command_id,
            CommandState.SUCCEEDED,
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )
        # 终态但审计未发布：不得清理。
        unpublished = seed_accept(repo, [_spec(point="unpub")], caller_id="op")[0]
        seed_transition(repo, unpublished.command_id, CommandState.CONNECTING)
        seed_transition(repo, unpublished.command_id, CommandState.EXECUTING)
        seed_transition(
            repo,
            unpublished.command_id,
            CommandState.FAILED,
            outcome=CommandOutcome.CONFIRMED_FAILURE,
        )
        # 未完成：不得清理。
        pending = seed_accept(repo, [_spec(point="pending")], caller_id="op")[0]
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


def test_buffered_admission_reserves_terminal_event_capacity() -> None:
    buffer = BufferedJournal(maximum_events=4)
    buffer.accept([_spec()], caller_id="operator")
    with pytest.raises(OverflowError, match="persistence queue full"):
        buffer.accept([_spec()], caller_id="operator")


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
    first = seed_accept(repo, [_spec()], caller_id="op")[0]
    seed_transition(repo, first.command_id, CommandState.CONNECTING)
    seed_transition(repo, first.command_id, CommandState.EXECUTING)
    seed_transition(
        repo,
        first.command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )
    seed_accept(repo, [_spec(point="b")], caller_id="op")
    metrics = repo.metrics()
    assert metrics.accepted_total == 2
    assert dict(metrics.terminal_counts).get("SUCCEEDED") == 1
    assert metrics.pending_events >= 3
    assert metrics.used_bytes > 0


async def test_command_control_maps_service_boundaries(
    tmp_path: Path, sqlite_writer: ThreadPoolExecutor
) -> None:
    from commander.application.persistence import BufferedJournal, PersistenceWorker

    repo = sqlite_writer.submit(
        lambda: EventRepository(tmp_path / "ctrl.db", retention_seconds=1800)
    ).result()
    buffer = BufferedJournal()
    worker = PersistenceWorker(buffer, repo, sqlite_writer, interval=0.02, batch_size=500)
    worker.start()
    try:
        control = CommandControl(repo, sqlite_writer, buffer)
        command = (await control.accept([_spec()], caller_id="op"))[0]
        await control.connecting(command.command_id)
        await control.executing(command.command_id)
        await control.complete(
            command.command_id,
            CommandState.UNKNOWN,
            outcome=CommandOutcome.UNKNOWN,
            error_code="WRITE_TIMEOUT",
        )
        # T1/T2/T3 在内存同步生效，调用方立即查询。
        result = _must(await control.get_command(command.command_id))
        assert result.state is CommandState.UNKNOWN
        assert result.error_code == "WRITE_TIMEOUT"
        assert await worker.stop(timeout=5.0)
        # 排空后持久层投影一致（UNKNOWN 终态事件落盘）。
        durable = sqlite_writer.submit(repo.get, command.command_id).result()
        assert durable is not None
        assert durable.state is CommandState.UNKNOWN
        assert durable.error_code == "WRITE_TIMEOUT"
    finally:
        sqlite_writer.submit(repo.close).result()


async def test_control_accept_is_memory_synchronous_even_when_writer_blocked(
    tmp_path: Path, sqlite_writer: ThreadPoolExecutor
) -> None:
    """write-behind 新契约：T1 不等待 SQLite。

    旧测试要求“writer 队列中的 T1 不可取消、必落盘”；新架构下 T1 在
    loop 内存中同步完成——即使唯一 writer 线程被占死，accept 立即返回、
    内存可查；落盘是后台 worker 的职责，崩溃允许丢失未落盘事件。
    """
    import threading

    from commander.application.persistence import BufferedJournal

    repo = sqlite_writer.submit(
        lambda: EventRepository(tmp_path / "cancel.db", retention_seconds=1800)
    ).result()
    release = threading.Event()
    sqlite_writer.submit(lambda: release.wait(2.0))  # 占住唯一 writer 线程
    try:
        control = CommandControl(repo, sqlite_writer, BufferedJournal())
        started = time.perf_counter()
        command = (await control.accept([_spec()], caller_id="op"))[0]
        assert time.perf_counter() - started < 0.05  # 不经过 writer 线程
        result = _must(await control.get_command(command.command_id))
        assert result.state is CommandState.ACCEPTED
    finally:
        release.set()
        sqlite_writer.submit(repo.close).result()
