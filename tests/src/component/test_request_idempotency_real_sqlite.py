"""请求级幂等契约（Commander v1_22 RR-01/RR-02）真实 SQLite 组件测试。

全程真实 EventRepository（WAL/FULL、flock）+ PersistenceWorker +
CommandControl 持久化门禁 + QueuedExecutor，仅现场协议侧使用受控
FakeSession；不模拟数据库、不以 Mock 冒充崩溃/恢复行为。

覆盖任务书语义项：
- ① request_id 非空/UTF-8 长度门禁与调用方命名空间隔离；
- ② 同 caller 同 ID 同内容仅执行一次，异内容冲突拒绝；
- ③ 指纹区分标量类型、顺序、期限、目标、版本；
- ⑪ 多调用方 request_id 隔离（含并发受理）；
- ⑦ 持久化不可用时门禁超时拒绝，无命令进入现场队列、零现场写入。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from commander.application.control import CommandControl
from commander.application.persistence import BufferedJournal, PersistenceWorker
from commander.application.queued_executor import QueuedExecutor
from commander.domain.command import AcceptedCommand, CommandSpec
from commander.infrastructure.persistence.event_repository import EventRepository
from tests.commander.component.conftest import FakeSession, build_executor, make_spec

pytestmark = [pytest.mark.component, pytest.mark.contract]


@dataclass
class _IdemStack:
    repository: EventRepository
    writer: ThreadPoolExecutor
    buffer: BufferedJournal
    worker: PersistenceWorker
    control: CommandControl
    executor: QueuedExecutor
    sessions: dict[str, FakeSession]


@pytest.fixture
async def idem_stack(tmp_path: Path) -> AsyncIterator[_IdemStack]:
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="src-idem-sqlite")
    repository = writer.submit(
        lambda: EventRepository(tmp_path / "idempotency.db", retention_seconds=1800)
    ).result()
    buffer = BufferedJournal()
    worker = PersistenceWorker(buffer, repository, writer, interval=0.02, batch_size=500)
    worker.start()
    control = CommandControl(repository, writer, buffer)
    sessions = {"WT01": FakeSession(), "WT02": FakeSession()}
    executor = build_executor(control, sessions)
    try:
        yield _IdemStack(repository, writer, buffer, worker, control, executor, sessions)
    finally:
        await executor.close()
        assert await worker.stop(timeout=10.0), "persistence worker did not drain on teardown"
        writer.submit(repository.close).result()
        writer.shutdown(wait=True)


def _rid() -> str:
    return f"req-idem-{uuid4().hex}"


async def _settle(stack: _IdemStack, accepted: tuple[AcceptedCommand, ...]) -> None:
    await asyncio.gather(*(stack.executor.wait(command.command_id) for command in accepted))


async def test_same_caller_same_id_same_content_executes_once(idem_stack: _IdemStack) -> None:
    """语义②：同 caller + 同 request_id + 同指纹，第二次受理返回原命令且不重复执行。"""
    request_id = _rid()
    specs = [make_spec("p1", device="WT01")]

    first = await idem_stack.executor.accept(specs, caller_id="ops-a", request_id=request_id)
    await _settle(idem_stack, first)
    writes_after_first = list(idem_stack.sessions["WT01"].one_calls)
    assert writes_after_first == ["p1"]

    replay = await idem_stack.executor.accept(specs, caller_id="ops-a", request_id=request_id)
    # 幂等返回原受理：同一 command_id，没有新命令进入队列。
    assert [c.command_id for c in replay] == [c.command_id for c in first]
    assert idem_stack.executor.pending_commands() == 0
    # 现场写入不重复。
    assert idem_stack.sessions["WT01"].one_calls == writes_after_first
    metrics = idem_stack.writer.submit(idem_stack.repository.metrics).result()
    assert metrics.accepted_total == 1


async def test_same_id_different_content_raises_conflict(idem_stack: _IdemStack) -> None:
    """语义②：同 caller + 同 request_id + 异指纹 → FileExistsError，原命令不被覆盖。"""
    request_id = _rid()
    first = await idem_stack.executor.accept(
        [make_spec("p1", device="WT01", value=1.0)],
        caller_id="ops-a",
        request_id=request_id,
    )
    with pytest.raises(FileExistsError):
        await idem_stack.executor.accept(
            [make_spec("p1", device="WT01", value=2.0)],
            caller_id="ops-a",
            request_id=request_id,
        )
    # 原受理仍可按 request_id 查询且内容不变。
    view = idem_stack.writer.submit(idem_stack.repository.get_request, request_id, "ops-a").result()
    assert [result.command.command_id for result in view] == [first[0].command_id]
    assert view[0].command.spec.value == 1.0


@pytest.mark.parametrize(
    ("mutated", "label"),
    [
        (CommandSpec("WT01", "p1", True, "ref-1"), "bool-vs-int"),
        (CommandSpec("WT01", "p1", 1.0, "ref-1"), "float-vs-int"),
        (CommandSpec("WT01", "p1", 1, "ref-2"), "revision"),
        (CommandSpec("WT02", "p1", 1, "ref-1"), "device"),
        (CommandSpec("WT01", "p2", 1, "ref-1"), "point"),
    ],
)
async def test_fingerprint_distinguishes_scalar_target_and_revision(
    idem_stack: _IdemStack, mutated: CommandSpec, label: str
) -> None:
    """语义③：标量类型（bool/int/float）、目标设备/点、配置版本各自进入指纹。"""
    request_id = f"req-fp-{label}-{uuid4().hex}"
    base = CommandSpec("WT01", "p1", 1, "ref-1")
    await idem_stack.executor.accept([base], caller_id="ops-a", request_id=request_id)
    with pytest.raises(FileExistsError):
        await idem_stack.executor.accept([mutated], caller_id="ops-a", request_id=request_id)


async def test_fingerprint_distinguishes_expiry(idem_stack: _IdemStack) -> None:
    """语义③：expires_at 纳入指纹；不同期限的同点同值互为冲突。"""
    request_id = _rid()
    near = datetime.now(UTC) + timedelta(seconds=30)
    far = datetime.now(UTC) + timedelta(seconds=300)
    await idem_stack.executor.accept(
        [CommandSpec("WT01", "p1", 1, "ref-1", expires_at=near)],
        caller_id="ops-a",
        request_id=request_id,
    )
    with pytest.raises(FileExistsError):
        await idem_stack.executor.accept(
            [CommandSpec("WT01", "p1", 1, "ref-1", expires_at=far)],
            caller_id="ops-a",
            request_id=request_id,
        )


async def test_fingerprint_distinguishes_command_order(idem_stack: _IdemStack) -> None:
    """语义③：批次内命令顺序是指纹的一部分，乱序重放不得当作同一请求。"""
    request_id = _rid()
    forward = [
        CommandSpec("WT01", "p1", 1, "ref-1"),
        CommandSpec("WT01", "p2", 2, "ref-1"),
    ]
    await idem_stack.executor.accept(forward, caller_id="ops-a", request_id=request_id)
    with pytest.raises(FileExistsError):
        await idem_stack.executor.accept(
            list(reversed(forward)), caller_id="ops-a", request_id=request_id
        )


async def test_request_id_namespace_isolated_per_caller(idem_stack: _IdemStack) -> None:
    """语义①/⑪：同一 request_id 在不同 caller 命名空间下是两个独立请求。"""
    request_id = _rid()
    specs = [make_spec("p1", device="WT01")]
    first = await idem_stack.executor.accept(specs, caller_id="ops-a", request_id=request_id)
    second = await idem_stack.executor.accept(specs, caller_id="ops-b", request_id=request_id)
    assert first[0].command_id != second[0].command_id

    view_a = idem_stack.writer.submit(
        idem_stack.repository.get_request, request_id, "ops-a"
    ).result()
    view_b = idem_stack.writer.submit(
        idem_stack.repository.get_request, request_id, "ops-b"
    ).result()
    assert [r.command.command_id for r in view_a] == [first[0].command_id]
    assert [r.command.command_id for r in view_b] == [second[0].command_id]
    # 跨命名空间查询彼此不可见。
    assert (
        idem_stack.writer.submit(
            idem_stack.repository.get_request, f"req-foreign-{uuid4().hex}", "ops-a"
        ).result()
        == ()
    )


async def test_concurrent_same_id_distinct_callers_both_admitted(
    idem_stack: _IdemStack,
) -> None:
    """语义⑪：多调用方并发使用同一 request_id，两边独立成功、零键冲突。"""
    request_id = _rid()
    specs = [make_spec("p1", device="WT01")]
    results = await asyncio.gather(
        idem_stack.executor.accept(specs, caller_id="ops-a", request_id=request_id),
        idem_stack.executor.accept(specs, caller_id="ops-b", request_id=request_id),
    )
    command_ids = {command.command_id for group in results for command in group}
    assert len(command_ids) == 2
    metrics = idem_stack.writer.submit(idem_stack.repository.metrics).result()
    assert metrics.accepted_total == 2


async def test_blank_and_oversized_request_id_rejected_before_persistence(
    idem_stack: _IdemStack,
) -> None:
    """语义①：空白 request_id 与超 128 UTF-8 字节的 ID 在受理门被拒，不落盘、零现场写入。"""
    specs = [make_spec("p1", device="WT01")]
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            await idem_stack.executor.accept(specs, caller_id="ops-a", request_id=blank)
    boundary = "é" * 64  # 64 个双字节字符 = 128 UTF-8 字节，恰好合法。
    assert len(boundary.encode("utf-8")) == 128
    accepted = await idem_stack.executor.accept(specs, caller_id="ops-a", request_id=boundary)
    await _settle(idem_stack, accepted)

    oversized = "é" * 64 + "x"  # 129 字节。
    assert len(oversized.encode("utf-8")) == 129
    with pytest.raises(ValueError):
        await idem_stack.executor.accept(specs, caller_id="ops-a", request_id=oversized)
    assert idem_stack.sessions["WT01"].one_calls == ["p1"]


async def test_persistence_outage_fails_admission_without_field_io(
    tmp_path: Path,
) -> None:
    """语义⑦：SQLite 永久故障时门禁超时 fail-closed，无队列条目、零现场写入。"""
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="src-idem-outage")
    repository = writer.submit(
        lambda: EventRepository(tmp_path / "outage.db", retention_seconds=1800)
    ).result()
    original_append = repository.append_events

    def faulting_append(events: object) -> None:
        raise sqlite3.OperationalError("simulated permanent SQLite outage")

    object.__setattr__(repository, "append_events", faulting_append)
    buffer = BufferedJournal()
    worker = PersistenceWorker(buffer, repository, writer, interval=0.02, batch_size=500)
    worker.start()
    control = CommandControl(repository, writer, buffer, accept_timeout=0.3)
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    try:
        with pytest.raises(TimeoutError):
            await executor.accept(
                [make_spec("p1", device="WT01")],
                caller_id="ops-a",
                request_id=f"req-outage-{uuid4().hex}",
            )
        assert executor.pending_commands() == 0
        assert session.one_calls == []
        # 内存保守终态：INTERRUPTED/NOT_SENT(PERSISTENCE_TIMEOUT)。
        cached = [
            buffer.get_cached(command_id)
            for command_id in {event["command_id"] for event in buffer.snapshot(500)}
        ]
        assert len(cached) == 1 and cached[0] is not None
        assert cached[0].state.name == "INTERRUPTED"
        assert cached[0].outcome is not None
        assert cached[0].outcome.name == "NOT_SENT"
        assert cached[0].error_code == "PERSISTENCE_TIMEOUT"
    finally:
        object.__setattr__(repository, "append_events", original_append)
        await executor.close()
        assert await worker.stop(timeout=10.0)
        writer.submit(repository.close).result()
        writer.shutdown(wait=True)
