"""诊断 Operation 取消与后台任务清理测试。

N-03 后点表诊断的在线读点改走 Collector 正式 ReadPoints（DeviceReadPort），
挂起假件因此实现在 reads 端口而非已退役的 Commander.verify_point。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.domain.identities import DeviceId
from web_server.application.device.diagnostic import DiagnosticService
from web_server.application.device.sample import PointValue
from web_server.application.operation.registry import OperationRegistry, OperationState

pytestmark = pytest.mark.unit


class _HangingReads:
    """挂起的 Collector read_point——永不返回，等待取消。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def read_points(self, device_id: str, point_ids: list[str]) -> list[PointValue]:
        raise AssertionError("point table test must use read_point")

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _service(reads: _HangingReads, operations: OperationRegistry) -> DiagnosticService:
    config = SimpleNamespace(
        current_config=SimpleNamespace(
            site=SimpleNamespace(devices={DeviceId("d1"): SimpleNamespace(device_model_id="m1")}),
            device_models={"m1": SimpleNamespace(point_table_id="t1")},
            point_tables={"t1": SimpleNamespace(points={"p1": object(), "p2": object()})},
        )
    )
    return DiagnosticService(
        commander=SimpleNamespace(),  # type: ignore[arg-type]
        reads=reads,
        control=None,  # type: ignore[arg-type]
        config=config,  # type: ignore[arg-type]
        operations=operations,
        network=None,  # type: ignore[arg-type]
    )


async def test_cancel_operation_marks_cancelled_and_stops_worker() -> None:
    operations = OperationRegistry()
    reads = _HangingReads()
    service = _service(reads, operations)

    operation = service.start_point_table_test("d1")
    await asyncio.wait_for(reads.started.wait(), timeout=1.0)

    record = service.cancel_operation(operation.operation_id)
    assert record.state is OperationState.CANCELLED

    # 底层协程必须真的被终止，而非仅改状态。
    task = service._background.copy().pop() if service._background else None
    if task is not None:
        with pytest.raises(asyncio.CancelledError):
            await task

    # 终态后进度不再推进，最终结果保持 cancelled。
    assert operations.get(operation.operation_id).state is OperationState.CANCELLED


def test_cancel_unknown_operation_raises_key_error() -> None:
    service = _service(_HangingReads(), OperationRegistry())
    with pytest.raises(KeyError):
        service.cancel_operation("missing")


async def test_cancel_terminal_operation_raises_value_error() -> None:
    operations = OperationRegistry()
    service = _service(_HangingReads(), operations)
    operation = service.start_point_table_test("d1")
    service.cancel_operation(operation.operation_id)
    with pytest.raises(ValueError, match="terminal"):
        service.cancel_operation(operation.operation_id)
    await service.aclose()


async def test_aclose_cancels_all_background_tasks() -> None:
    operations = OperationRegistry()
    reads = _HangingReads()
    service = _service(reads, operations)
    operation = service.start_point_table_test("d1")
    # 必须等到 worker 实际进入挂起读点（mark_running 已执行），否则任务可能
    # 在首个调度步之前就被取消，Operation 仍停在 PENDING。
    await asyncio.wait_for(reads.started.wait(), timeout=1.0)

    await service.aclose()

    assert not service._background
    assert operations.get(operation.operation_id).state is OperationState.CANCELLED
