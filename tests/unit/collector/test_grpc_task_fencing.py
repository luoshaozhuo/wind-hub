"""Collector RPC placement 所有权栅栏的拒绝、续期、撤销测试。"""

from unittest.mock import AsyncMock, Mock

import grpc
import pytest

from collector.infrastructure.grpc.server import CollectorControlService
from core.infrastructure.rpc import collector_pb2 as pb


def _server() -> CollectorControlService:
    app = Mock()
    app.runtime.task_definitions.return_value = {"a": object(), "task-1": object()}
    service = CollectorControlService(app, "collector-a")
    service._tasks = Mock()
    service._tasks.stop_task = AsyncMock()
    return service


def _context() -> Mock:
    ctx = Mock()
    ctx.abort = AsyncMock(side_effect=RuntimeError("aborted"))
    return ctx


@pytest.mark.asyncio
async def test_reject_start_without_placement() -> None:
    server, ctx = _server(), _context()
    with pytest.raises(RuntimeError, match="aborted"):
        await server.StartTask(
            pb.TaskStartRequest(task_id="task-1", placement_generation=1), ctx
        )
    ctx.abort.assert_awaited_once_with(
        grpc.StatusCode.FAILED_PRECONDITION,
        "task placement ownership mismatch",
    )


@pytest.mark.asyncio
async def test_placement_identity_and_generation_checks() -> None:
    server, ctx = _server(), _context()
    with pytest.raises(RuntimeError, match="aborted"):
        await server.ApplyTaskPlacement(
            pb.TaskPlacementSnapshotRequest(
                worker_id="collector-b", generation=1, task_ids=["a"]
            ),
            ctx,
        )
    ctx.abort.assert_awaited_with(
        grpc.StatusCode.PERMISSION_DENIED, "collector identity mismatch"
    )


@pytest.mark.asyncio
async def test_empty_placement_is_valid() -> None:
    server = _server()
    response = await server.ApplyTaskPlacement(
        pb.TaskPlacementSnapshotRequest(worker_id="collector-a", generation=1),
        _context(),
    )
    assert response.success
    assert response.task_count == 0


@pytest.mark.asyncio
async def test_revocation_stops_removed_task() -> None:
    server = _server()
    context = _context()
    await server.ApplyTaskPlacement(
        pb.TaskPlacementSnapshotRequest(
            worker_id="collector-a", generation=1, task_ids=["task-1"]
        ),
        context,
    )
    result = await server.ApplyTaskPlacement(
        pb.TaskPlacementSnapshotRequest(
            worker_id="collector-a", generation=2
        ),
        context,
    )
    assert result.task_count == 0
    server._tasks.stop_task.assert_awaited_once_with("task-1")
    with pytest.raises(RuntimeError, match="aborted"):
        await server._require_task_ownership("task-1", 2, context)


@pytest.mark.asyncio
async def test_same_generation_conflicting_snapshot_rejected() -> None:
    server, ctx = _server(), _context()
    await server.ApplyTaskPlacement(
        pb.TaskPlacementSnapshotRequest(worker_id="collector-a", generation=1, task_ids=["a"]),
        ctx,
    )
    with pytest.raises(RuntimeError, match="aborted"):
        await server.ApplyTaskPlacement(
            pb.TaskPlacementSnapshotRequest(worker_id="collector-a", generation=1, task_ids=["b"]),
            ctx,
        )
    ctx.abort.assert_awaited_with(
        grpc.StatusCode.FAILED_PRECONDITION, "placement generation conflict"
    )
