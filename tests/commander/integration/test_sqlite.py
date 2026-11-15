"""§14.3 test_sqlite.py（integration/reliability）：真实 SQLite 事务、

提交失败整批回滚、worker 重试幂等、数据库重开恢复、flock 单写者与
Outbox 顺序/去重/清理。全程使用磁盘上的真实 WAL/FULL 数据库（无内存替身）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from commander.application.persistence import BufferedJournal
from commander.domain.command import (
    CommandOutcome,
    CommandSpec,
    CommandState,
)
from commander.infrastructure.persistence.event_repository import (
    EventRepository,
    RepositoryError,
)

from ..seed_events import seed_accept, seed_transition

pytestmark = [
    pytest.mark.integration,
    pytest.mark.reliability,
    pytest.mark.real_service,
]


def _spec(point: str = "setpoint") -> CommandSpec:
    return CommandSpec("WT01", point, 12.5, "ref-abc")


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[EventRepository]:
    repository = EventRepository(tmp_path / "commander.db", retention_seconds=1800)
    try:
        yield repository
    finally:
        repository.close()


def _row_count(repository: EventRepository) -> int:
    return repository._db.execute("SELECT COUNT(*) FROM command_events").fetchone()[0]  # noqa: SLF001


# ---------------------------------------------------------------------------
# storage：真实文件、WAL/FULL、重开还原
# ---------------------------------------------------------------------------


@pytest.mark.storage
@pytest.mark.normal
def test_pragmas_wal_and_full_synchronous_on_real_file(repo: EventRepository) -> None:
    mode = repo._db.execute("PRAGMA journal_mode").fetchone()[0]  # noqa: SLF001
    assert mode.lower() == "wal"
    assert repo._db.execute("PRAGMA synchronous").fetchone()[0] in (2, 3)  # noqa: SLF001
    assert Path(str(repo._path) + "-wal").exists() or repo._path.exists()  # noqa: SLF001


@pytest.mark.storage
@pytest.mark.recovery
def test_reopen_restores_full_command_history(repo: EventRepository) -> None:
    accepted = seed_accept(repo, [_spec()], caller_id="ops")
    seed_transition(repo, accepted[0].command_id, CommandState.CONNECTING)
    seed_transition(
        repo,
        accepted[0].command_id,
        CommandState.EXECUTING,
    )
    seed_transition(
        repo,
        accepted[0].command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )
    path = repo._path  # noqa: SLF001
    repo.close()

    reopened = EventRepository(path)
    try:
        result = reopened.get(accepted[0].command_id)
        assert result is not None
        assert result.state is CommandState.SUCCEEDED
        assert result.outcome is CommandOutcome.CONFIRMED_SUCCESS
        assert result.completed_at is not None
        assert reopened.latest_receive_seq() == accepted[0].receive_seq
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# recovery：进程崩溃后保守裁决，且绝不重发（恢复逻辑不做任何现场 I/O）
# ---------------------------------------------------------------------------


@pytest.mark.recovery
@pytest.mark.exception
def test_recover_missing_t2_evidence_is_conservatively_unknown(
    repo: EventRepository,
) -> None:
    """Commander v1_22 §9.5：缺持久 T2 不证明未发送，两类未完结命令均裁决 UNKNOWN。"""
    executing = seed_accept(repo, [_spec("a")], caller_id="ops")
    seed_transition(repo, executing[0].command_id, CommandState.CONNECTING)
    seed_transition(repo, executing[0].command_id, CommandState.EXECUTING)
    accepted_only = seed_accept(repo, [_spec("b")], caller_id="ops")
    path = repo._path  # noqa: SLF001
    repo.close()

    reopened = EventRepository(path)
    try:
        recovered = reopened.recover()
        assert set(recovered) == {executing[0].command_id, accepted_only[0].command_id}
        first = reopened.get(executing[0].command_id)
        second = reopened.get(accepted_only[0].command_id)
        assert first is not None and second is not None
        # 已见 EXECUTING：可能已写入，保守 UNKNOWN。
        assert first.state is CommandState.UNKNOWN
        assert first.outcome is CommandOutcome.UNKNOWN
        # 仅有 ACCEPTED：缓冲 T2 可能在崩溃前已触发物理 I/O，同样不得判 NOT_SENT。
        assert second.state is CommandState.UNKNOWN
        assert second.outcome is CommandOutcome.UNKNOWN
        # 恢复幂等：第二次没有新裁决。
        assert reopened.recover() == ()
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# exception/concurrency：单写者锁、整批原子、重放幂等
# ---------------------------------------------------------------------------


@pytest.mark.exception
def test_second_writer_on_same_file_is_rejected_by_flock(
    repo: EventRepository, tmp_path: Path
) -> None:
    with pytest.raises(RepositoryError, match="owned by another process"):
        EventRepository(repo._path)  # noqa: SLF001


@pytest.mark.exception
def test_batch_with_check_violation_rolls_back_entire_commit(
    repo: EventRepository,
) -> None:
    """ON CONFLICT DO NOTHING 只吞唯一冲突；CHECK 违约必须整批回滚。"""
    buffer = BufferedJournal()
    buffer.accept([_spec()], caller_id="ops", request_id="req-check-violation")
    events = buffer.snapshot(500)
    repo.append_events(events)
    before = _row_count(repo)

    # 构造混合批次：一个合法但重复的事件 + 一个 event_type CHECK 违约事件。
    duplicate = dict(events[0])
    bogus = dict(events[0])
    bogus["event_id"] = "bogus-event-id"
    bogus["event_type"] = "BOGUS_TYPE"
    with pytest.raises(RepositoryError, match="retryable"):
        repo.append_events([duplicate, bogus])
    # 重复行没有"部分提交"，违约行不存在，总数不变。
    assert _row_count(repo) == before
    persisted = repo._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM command_events WHERE event_id=?",
        ("bogus-event-id",),
    ).fetchone()[0]
    assert persisted == 0


@pytest.mark.recovery
def test_replaying_same_batch_is_idempotent(repo: EventRepository) -> None:
    """worker 重试（同一 event_id 集合）不得产生重复行。"""
    buffer = BufferedJournal()
    commands = buffer.accept(
        [_spec("a"), _spec("b")], caller_id="ops", request_id="req-replay-idempotent"
    )
    events = buffer.snapshot(500)
    repo.append_events(events)
    repo.append_events([dict(item) for item in events])  # 模拟重试重投
    rows = _row_count(repo)
    repo.append_events([dict(item) for item in events])
    assert _row_count(repo) == rows
    request = repo.get_request(commands[0].request_id, "ops")
    assert len(request) == 2


# ---------------------------------------------------------------------------
# storage：Outbox 顺序、发布标记与保留期清理
# ---------------------------------------------------------------------------


@pytest.mark.storage
@pytest.mark.normal
def test_outbox_pending_order_and_publish_marking(repo: EventRepository) -> None:
    accepted = seed_accept(repo, [_spec()], caller_id="ops")
    seed_transition(repo, accepted[0].command_id, CommandState.CONNECTING)
    seed_transition(repo, accepted[0].command_id, CommandState.EXECUTING)
    seed_transition(
        repo,
        accepted[0].command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )
    pending = repo.pending_events(limit=10)
    assert [item["event_type"] for item in pending] == [
        "ACCEPTED",
        "CONNECTING",
        "EXECUTING",
        "COMPLETED",
    ]
    repo.mark_published(pending[0]["event_id"])
    remaining = repo.pending_events(limit=10)
    assert all(item["publish_state"] == "PENDING" for item in remaining)
    assert pending[0]["event_id"] not in {item["event_id"] for item in remaining}
    # 重复标记是无操作（不报错、不重复）。
    repo.mark_published(pending[0]["event_id"])


@pytest.mark.storage
@pytest.mark.exception
def test_cleanup_deletes_only_old_fully_published_requests(
    repo: EventRepository,
) -> None:
    old = seed_accept(repo, [_spec("old")], caller_id="ops")
    seed_transition(
        repo,
        old[0].command_id,
        CommandState.FAILED,
        outcome=CommandOutcome.CONFIRMED_FAILURE,
    )
    fresh = seed_accept(repo, [_spec("fresh")], caller_id="ops")
    seed_transition(
        repo,
        fresh[0].command_id,
        CommandState.FAILED,
        outcome=CommandOutcome.CONFIRMED_FAILURE,
    )
    # 清理要求 PENDING 发箱为空：全部发布后仅旧请求过期。
    for item in repo.pending_events(limit=100):
        repo.mark_published(item["event_id"])
    repo._db.execute(  # noqa: SLF001
        "UPDATE command_events SET occurred_at=? WHERE command_id=?",
        ("2000-01-01T00:00:00+00:00", old[0].command_id),
    )
    repo._db.commit()  # noqa: SLF001
    deleted = repo.cleanup(limit=100)
    assert deleted == 1
    assert repo.get(old[0].command_id) is None
    assert repo.get(fresh[0].command_id) is not None


@pytest.mark.exception
def test_append_after_close_is_repository_error(tmp_path: Path) -> None:
    """关停后到达的落盘批次必须报 RepositoryError，而不是静默丢失。"""
    path = tmp_path / "commander.db"
    repository = EventRepository(path)
    repository.close()
    assert repository.operational is False
    with pytest.raises(RepositoryError, match="unavailable"):
        repository.append_events(
            [
                {
                    "event_id": "x",
                    "command_id": "c",
                    "event_type": "ACCEPTED",
                    "state": "ACCEPTED",
                }
            ]
        )
