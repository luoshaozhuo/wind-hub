"""Commander 测试共享夹具：唯一 writer 线程、线程内创建的真实 SQLite 仓储。

write-behind 架构下装配与生产一致：
  BufferedJournal（loop 内内存状态）→ PersistenceWorker（周期/阈值批量
  落盘）→ EventRepository（唯一 writer 线程归属的真实 SQLite WAL/FULL）。
内存状态由 CommandControl 读写；worker 把不可变事件快照单事务幂等落盘。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from commander.application.control import CommandControl
from commander.application.persistence import BufferedJournal, PersistenceWorker
from commander.infrastructure.event_repository import EventRepository


@pytest.fixture
def sqlite_writer() -> Iterator[ThreadPoolExecutor]:
    """与生产装配一致的唯一单线程 SQLite 执行器。"""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-sqlite")
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


@pytest.fixture
def repository(tmp_path: Path, sqlite_writer: ThreadPoolExecutor) -> Iterator[EventRepository]:
    # 连接必须在其归属线程（writer）内创建。
    repo = sqlite_writer.submit(lambda: EventRepository(tmp_path / "commander.db")).result()
    try:
        yield repo
    finally:
        sqlite_writer.submit(repo.close).result()


@pytest.fixture
def buffer() -> BufferedJournal:
    return BufferedJournal()


@pytest.fixture
async def persistence(
    buffer: BufferedJournal, repository: EventRepository, sqlite_writer: ThreadPoolExecutor
) -> AsyncIterator[PersistenceWorker]:
    """生产同模型的后台落盘 worker；拆卸时有界排空剩余内存事件。"""
    worker = PersistenceWorker(buffer, repository, sqlite_writer, interval=0.02, batch_size=500)
    worker.start()
    try:
        yield worker
    finally:
        # 先于 repository 夹具关闭：排空后 SQLite 侧事件齐全。
        assert await worker.stop(timeout=10.0), "persistence worker did not drain on teardown"


@pytest.fixture
def control(
    repository: EventRepository,
    sqlite_writer: ThreadPoolExecutor,
    buffer: BufferedJournal,
    persistence: PersistenceWorker,
) -> CommandControl:
    return CommandControl(repository, sqlite_writer, buffer)


async def flush_buffer(
    buffer: BufferedJournal, worker: PersistenceWorker, *, timeout: float = 5.0
) -> None:
    """确定性等待全部内存事件落盘且 worker 无在途失败。"""
    deadline = time.monotonic() + timeout
    while buffer.pending_count or worker.failure is not None:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"buffer did not flush: pending={buffer.pending_count} "
                f"failure={worker.failure!r}"
            )
        await asyncio.sleep(0.005)
