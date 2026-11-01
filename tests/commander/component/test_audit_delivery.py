"""AuditDelivery 组件测试：发箱顺序、至少一次、退避与关闭边界。

仓储用真实 EventRepository（归属唯一 SQLite writer 线程，与生产装配一致）；
传输层用受控替身（JetStream 真实链路在 integration 层验证）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from commander.application.audit import AuditDelivery
from commander.domain.command import (
    CommandOutcome,
    CommandSpec,
    CommandState,
)
from commander.infrastructure.event_repository import EventRepository

pytestmark = pytest.mark.component


class ScriptedTransport:
    def __init__(
        self,
        *,
        fail_times: int = 0,
        hang: asyncio.Event | None = None,
        always_fail: bool = False,
    ) -> None:
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.hang = hang
        self.published: list[dict[str, Any]] = []
        self.closed = False

    async def publish(self, event: Mapping[str, Any]) -> None:
        if self.always_fail or self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("jetstream unavailable")
        if self.hang is not None:
            await self.hang.wait()
        self.published.append(dict(event))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def sqlite_writer() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-audit-sqlite")
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


@pytest.fixture
def repo(db_path: Path, sqlite_writer: ThreadPoolExecutor) -> Iterator[EventRepository]:
    repository = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    try:
        yield repository
    finally:
        sqlite_writer.submit(repository.close).result()


def _four_events(repo: EventRepository, writer: ThreadPoolExecutor) -> list[str]:
    def write() -> list[str]:
        command = repo.accept([CommandSpec("WT01", "p", 1.0, "ref")], caller_id="op")[0]
        repo.transition(command.command_id, CommandState.CONNECTING)
        repo.transition(command.command_id, CommandState.EXECUTING)
        repo.transition(
            command.command_id,
            CommandState.SUCCEEDED,
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )
        return [event["event_id"] for event in repo.pending_events(10)]

    return writer.submit(write).result()


async def _wait_drained(repo: EventRepository, writer: ThreadPoolExecutor) -> None:
    for _ in range(300):
        if not writer.submit(repo.pending_events, 100).result():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("audit outbox did not drain")


async def test_publishes_in_event_seq_order_and_marks_outbox(
    repo: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> None:
    event_ids = _four_events(repo, sqlite_writer)
    transport = ScriptedTransport()
    delivery = AuditDelivery(repo, transport, sqlite_writer, retry_base=0.01, retry_max=0.05)
    delivery.start()
    try:
        await asyncio.wait_for(_wait_drained(repo, sqlite_writer), timeout=2.0)
        assert await delivery.stop(timeout=2.0)
    finally:
        if delivery.operational:
            await delivery.stop(timeout=2.0)
    assert [event["event_id"] for event in transport.published] == event_ids
    assert delivery.published == 4


async def test_transport_failure_retries_same_event_and_records_attempts(
    repo: EventRepository, db_path: Path, sqlite_writer: ThreadPoolExecutor
) -> None:
    event_ids = _four_events(repo, sqlite_writer)
    transport = ScriptedTransport(fail_times=3)
    delivery = AuditDelivery(repo, transport, sqlite_writer, retry_base=0.01, retry_max=0.05)
    delivery.start()
    try:
        await asyncio.wait_for(_wait_drained(repo, sqlite_writer), timeout=3.0)
        assert await delivery.stop(timeout=2.0)
    finally:
        if delivery.operational:
            await delivery.stop(timeout=2.0)
    assert delivery.failures == 3
    # 失败期间不得跳过队首；最终顺序仍严格按 event_seq。
    assert [event["event_id"] for event in transport.published] == event_ids
    raw = sqlite3.connect(db_path)
    try:
        attempts = raw.execute(
            "SELECT attempts FROM command_events WHERE event_id=?", (event_ids[0],)
        ).fetchone()[0]
        states = raw.execute("SELECT DISTINCT publish_state FROM command_events").fetchall()
    finally:
        raw.close()
    assert attempts == 3
    assert states == [("PUBLISHED",)]


async def test_stop_returns_false_when_publisher_still_in_io(
    repo: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> None:
    _four_events(repo, sqlite_writer)
    release = asyncio.Event()
    transport = ScriptedTransport(hang=release)
    delivery = AuditDelivery(repo, transport, sqlite_writer, retry_base=0.01)
    delivery.start()
    await asyncio.sleep(0.05)
    try:
        # I/O 未结束：有界关闭必须如实返回 False，且在途任务不被取消。
        assert await delivery.stop(timeout=0.1) is False
        assert delivery.operational is True  # shield 保护的发布仍在途
    finally:
        release.set()
        # 发布返回后 worker 因停机 deadline 已过而自行终止（不被遗忘）。
        for _ in range(100):
            if not delivery.operational:
                break
            await asyncio.sleep(0.01)
        assert delivery.operational is False


async def test_persistent_failure_during_shutdown_returns_false_and_keeps_pending(
    repo: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> None:
    """持续不可用：重试受停机总期限约束，超时 False，PENDING 全部保留。"""
    _four_events(repo, sqlite_writer)
    transport = ScriptedTransport(always_fail=True)
    delivery = AuditDelivery(repo, transport, sqlite_writer, retry_base=0.01, retry_max=0.02)
    delivery.start()
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    assert await delivery.stop(timeout=0.5) is False
    assert asyncio.get_running_loop().time() - started < 3.0
    assert delivery.operational is False
    pending = sqlite_writer.submit(repo.pending_events, 100).result()
    assert len(pending) == 4


async def test_tail_events_inserted_during_stop_are_drained(
    repo: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> None:
    """最后一个事件在发布在途时写入：停机排空不得丢失末尾事件。"""
    first_ids = _four_events(repo, sqlite_writer)
    release = asyncio.Event()
    transport = ScriptedTransport(hang=release)
    delivery = AuditDelivery(repo, transport, sqlite_writer, retry_base=0.01)
    delivery.start()
    await asyncio.sleep(0.05)
    try:
        before = {
            event["event_id"] for event in sqlite_writer.submit(repo.pending_events, 100).result()
        }

        # 在途期间再写一条完整命令链（模拟终态事件与停机并发）。
        def write_tail() -> list[str]:
            tail = repo.accept([CommandSpec("WT01", "p", 2.0, "ref")], caller_id="op")[0]
            repo.transition(tail.command_id, CommandState.CONNECTING)
            repo.transition(tail.command_id, CommandState.EXECUTING)
            repo.transition(
                tail.command_id,
                CommandState.SUCCEEDED,
                outcome=CommandOutcome.CONFIRMED_SUCCESS,
            )
            return [
                event["event_id"]
                for event in repo.pending_events(100)
                if event["event_id"] not in before
            ]

        tail_ids = sqlite_writer.submit(write_tail).result()
        assert len(tail_ids) == 4
        stop_task = asyncio.create_task(delivery.stop(timeout=5.0))
        await asyncio.sleep(0.05)
        release.set()
        assert await stop_task is True
    finally:
        release.set()
        if delivery.operational:
            await delivery.stop(timeout=2.0)
    assert not sqlite_writer.submit(repo.pending_events, 100).result()
    assert [event["event_id"] for event in transport.published] == first_ids + tail_ids


async def test_transient_outage_recovers_within_stop_budget(
    repo: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> None:
    """B3-P1：停机窗口内 NATS 短暂中断后恢复，必须在预算内排空而非 worker 死亡。"""
    _four_events(repo, sqlite_writer)

    class FlappingTransport:
        def __init__(self) -> None:
            self.published: list[dict[str, Any]] = []
            self.failed = False

        async def publish(self, event: Mapping[str, Any]) -> None:
            # 留出窗口让 stop 在首轮发布期间置位。
            await asyncio.sleep(0.05)
            if not self.failed:
                self.failed = True
                raise ConnectionError("jetstream briefly unavailable")
            self.published.append(dict(event))

        async def close(self) -> None:
            return None

    flapping = FlappingTransport()
    delivery = AuditDelivery(repo, flapping, sqlite_writer, retry_base=0.05, retry_max=0.1)
    delivery.start()
    try:
        stop_task = asyncio.create_task(delivery.stop(timeout=5.0))
        # 恢复后应在预算内继续排空，而不是 worker 死亡、stop 返回 False。
        assert await stop_task is True
        assert not sqlite_writer.submit(repo.pending_events, 100).result()
    finally:
        if delivery.operational:
            await delivery.stop(timeout=2.0)
    assert delivery.failures == 1
    assert len(flapping.published) == 4


async def test_invalid_retry_construction_rejected(
    repo: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> None:
    with pytest.raises(ValueError):
        AuditDelivery(repo, ScriptedTransport(), sqlite_writer, retry_base=0, retry_max=0.05)
    with pytest.raises(ValueError):
        AuditDelivery(repo, ScriptedTransport(), sqlite_writer, retry_base=0.1, retry_max=0.01)
