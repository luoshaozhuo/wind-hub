"""AuditDelivery 组件测试：发箱顺序、至少一次、退避与关闭边界。

仓储用真实 EventRepository；传输层用受控替身（JetStream 真实链路在
integration 层验证）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Mapping
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
    ) -> None:
        self.fail_times = fail_times
        self.hang = hang
        self.published: list[dict[str, Any]] = []
        self.closed = False

    async def publish(self, event: Mapping[str, Any]) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("jetstream unavailable")
        if self.hang is not None:
            await self.hang.wait()
        self.published.append(dict(event))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


@pytest.fixture
def repo(db_path: Path) -> Iterator[EventRepository]:
    repository = EventRepository(db_path)
    yield repository
    repository.close()


def _four_events(repo: EventRepository) -> list[str]:
    command = repo.accept([CommandSpec("WT01", "p", 1.0, "ref")], caller_id="op")[0]
    repo.transition(command.command_id, CommandState.CONNECTING)
    repo.transition(command.command_id, CommandState.EXECUTING)
    repo.transition(
        command.command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )
    return [event["event_id"] for event in repo.pending_events(10)]


async def _wait_drained(repo: EventRepository, count: int) -> None:
    for _ in range(300):
        if not repo.pending_events(100):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"audit outbox did not drain; {count}")


async def test_publishes_in_event_seq_order_and_marks_outbox(repo: EventRepository) -> None:
    event_ids = _four_events(repo)
    transport = ScriptedTransport()
    delivery = AuditDelivery(repo, transport, retry_base=0.01, retry_max=0.05)
    delivery.start()
    try:
        await asyncio.wait_for(_wait_drained(repo, 4), timeout=2.0)
        assert await delivery.stop(timeout=2.0)
    finally:
        if delivery.operational:
            await delivery.stop(timeout=2.0)
    assert [event["event_id"] for event in transport.published] == event_ids
    assert delivery.published == 4


async def test_transport_failure_retries_same_event_and_records_attempts(
    repo: EventRepository, db_path: Path
) -> None:
    event_ids = _four_events(repo)
    transport = ScriptedTransport(fail_times=3)
    delivery = AuditDelivery(repo, transport, retry_base=0.01, retry_max=0.05)
    delivery.start()
    try:
        await asyncio.wait_for(_wait_drained(repo, 4), timeout=3.0)
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


async def test_stop_returns_false_when_publisher_still_in_io(repo: EventRepository) -> None:
    _four_events(repo)
    release = asyncio.Event()
    transport = ScriptedTransport(hang=release)
    delivery = AuditDelivery(repo, transport, retry_base=0.01)
    delivery.start()
    await asyncio.sleep(0.05)
    try:
        # I/O 未结束：有界关闭必须如实返回 False，绝不取消任务。
        assert await delivery.stop(timeout=0.1) is False
    finally:
        release.set()
        await asyncio.sleep(0.05)


async def test_invalid_retry_construction_rejected(repo: EventRepository) -> None:
    with pytest.raises(ValueError):
        AuditDelivery(repo, ScriptedTransport(), retry_base=0, retry_max=0.05)
    with pytest.raises(ValueError):
        AuditDelivery(repo, ScriptedTransport(), retry_base=0.1, retry_max=0.01)
