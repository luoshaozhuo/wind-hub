"""Commander 测试共享夹具：唯一 writer 线程、线程内创建的真实 SQLite 仓储。

生产装配中 EventRepository 在 commander-sqlite 单线程池内创建和关闭；
SQLite 连接有线程亲和性（check_same_thread），测试必须保持同一归属，
否则 writer 执行 T1/T2/T3 会触发 ProgrammingError。
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from commander.application.control import CommandControl
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
def control(repository: EventRepository, sqlite_writer: ThreadPoolExecutor) -> CommandControl:
    return CommandControl(repository, sqlite_writer)
