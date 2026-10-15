"""诊断 Operation 取消与后台任务清理测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.domain.identities import DeviceId
from web_server.application.device.diagnostic import DiagnosticService
from web_server.application.operation.registry import OperationRegistry, OperationState


class _Commander:
    """挂起的 verify_point——永不返回，等待取消。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def verify_point(self, device_id: str, point_id: str) -> Any:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _service(commander: _Commander, operations: OperationRegistry) -> DiagnosticService:
    config = SimpleNamespace(
        current_config=SimpleNamespace(
            site=SimpleNamespace(devices={DeviceId("d1"): SimpleNamespace(device_model_id="m1")}),
            device_models={"m1": SimpleNamespace(point_table_id="t1")},
            point_tables={"t1": SimpleNamespace(points={"p1": object(), "p2": object()})},
        )
    )
    return DiagnosticService(
        commander=commander,  # type: ignore[arg-type]
        control=None,  # type: ignore[arg-type]
        config=config,  # type: ignore[arg-type]
        operations=operations,
        network=None,  # type: ignore[arg-type]
    )


async def test_cancel_operation_marks_cancelled_and_stops_worker() -> None:
    operations = OperationRegistry()
    commander = _Commander()
    service = _service(commander, operations)

    operation = service.start_point_table_test("d1")
    await asyncio.wait_for(commander.started.wait(), timeout=1.0)

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
    service = _service(_Commander(), OperationRegistry())
    with pytest.raises(KeyError):
        service.cancel_operation("missing")


async def test_cancel_terminal_operation_raises_value_error() -> None:
    operations = OperationRegistry()
    service = _service(_Commander(), operations)
    operation = service.start_point_table_test("d1")
    service.cancel_operation(operation.operation_id)
    with pytest.raises(ValueError, match="terminal"):
        service.cancel_operation(operation.operation_id)
    await service.aclose()


async def test_aclose_cancels_all_background_tasks() -> None:
    operations = OperationRegistry()
    service = _service(_Commander(), operations)
    operation = service.start_point_table_test("d1")
    await asyncio.wait_for(_event_or_timeout(service), timeout=1.0)

    await service.aclose()

    assert not service._background
    assert operations.get(operation.operation_id).state is OperationState.CANCELLED


async def _event_or_timeout(service: DiagnosticService) -> None:
    """等待 worker 启动（借用 commander started 事件不可达时退化为短暂让渡）。"""
    for _ in range(100):
        if service._background:
            return
        await asyncio.sleep(0)
