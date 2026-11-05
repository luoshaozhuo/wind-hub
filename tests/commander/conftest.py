"""Commander 测试共享夹具：唯一 writer 线程、线程内创建的真实 SQLite 仓储。

write-behind 架构下装配与生产一致：
  BufferedJournal（loop 内内存状态）→ PersistenceWorker（周期/阈值批量
  落盘）→ EventRepository（唯一 writer 线程归属的真实 SQLite WAL/FULL）。
内存状态由 CommandControl 读写；worker 把不可变事件快照单事务幂等落盘。

另含 v1_10 §14.2 场景/专题 Marker 的集中补标：规则按下表显式匹配测试
函数名关键词（与根 conftest 按路径补层级/协议 marker 的惯例一致），
文件内 ``pytestmark`` 或函数装饰器的显式声明优先且不会被重复添加。
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

# 专题 marker 的文件名归属（不含目录前缀与 .py 后缀）。
_RELIABILITY_FILES = {
    "test_command_state",
    "test_queued_executor",
    "test_event_repository",
    "test_sqlite_writer_concurrency",
    "test_audit_delivery",
    "test_persistence_service_health",
    "test_hot_reload",
    "test_sqlite",
    "test_nats_jetstream_real",
    "test_audit_shutdown_real",
    "test_modbus_real_link",
    "test_service_e2e_real_stack",
    "test_lifecycle",
    "test_control_flow",
    "test_concurrency",
    "test_crash_recovery",
    "test_capacity",
    "test_field_protocol",
}
_SECURITY_FILES = {"test_auth", "test_grpc", "test_common_service", "test_commander_service"}
_PERFORMANCE_FILES = {"test_performance"}

# 场景 marker 的函数名关键词（小写包含匹配；按 marker 列出）。
_SCENARIO_KEYWORDS = {
    "timeout": (
        "timeout",
        "deadline",
        "expire",
        "expiration",
        "expired",
        "outlast",
        "hanging",
        "unsettled_io",
    ),
    "recovery": (
        "recover",
        "recovery",
        "restart",
        "crash",
        "retry",
        "retries",
        "transient",
        "reopen",
        "rolls_back",
        "rollback",
        "redeye",
    ),
    "concurrency": (
        "concurrent",
        "fifo",
        "parallel",
        "race",
        "quota",
        "coalesce",
        "serial",
        "back_to_back",
        "multi",
    ),
    "storage": (
        "sqlite",
        "storage",
        "outbox",
        "persist",
        "durable",
        "cleanup",
        "capacity",
        "journal",
        "repository",
        "flock",
        "pragmas",
    ),
    "load": ("throughput", "100_devices", "nominal_load", "_load_"),
    "exception": (
        "reject",
        "rejected",
        "invalid",
        "illegal",
        "failure",
        "failed",
        "fail",
        "error",
        "denied",
        "outage",
        "unknown",
        "missing",
        "mismatch",
        "bad",
        "corrupt",
        "violation",
        "cannot",
        "unavailable",
        "conflict",
        "unsafe",
        "stale",
        "escaping",
        "noncanonical",
        "malformed",
        "broken",
        "duplicate",
        "boundary",
        "past",
        "bytes_kind",
        "unset",
        "stranger",
        "untrusted",
        "immutable",
        "single_use",
        "external",
        "exception",
    ),
    "normal": (
        "happy",
        "full_chain",
        "callable",
        "registered",
        "scopes",
        "info_status",
        "start_ready",
        "submit_and_query",
        "devices_and_points",
        "pagination",
        "lifecycle",
        "graceful",
        "order",
        "idempotent",
        "no_op",
        "noop",
        "restores",
        "validates",
        "valid",
        "numeric_kinds",
        "service_yaml_change",
        "float_point",
        "walk",
        "five_supported",
    ),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """按 §14.2 词汇表给 commander 测试补专题/场景 marker（不重复添加）。"""
    for item in items:
        try:
            parts = item.path.relative_to(Path(__file__).resolve().parent.parent).parts
        except ValueError:
            continue
        if parts[0] != "commander":
            continue
        existing = {marker.name for marker in item.iter_markers()}
        stem = item.path.stem
        name = item.name.lower()

        def add(marker: str, *, _item=item, _existing=existing) -> None:
            if marker not in _existing:
                _item.add_marker(getattr(pytest.mark, marker))

        if stem in _RELIABILITY_FILES:
            add("reliability")
        if stem in _SECURITY_FILES:
            add("security")
        if stem in _PERFORMANCE_FILES:
            add("performance")
        for marker, keywords in _SCENARIO_KEYWORDS.items():
            if any(keyword in name for keyword in keywords):
                add(marker)


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
