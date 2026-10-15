"""Collector gRPC 控制面必须拒绝未受所有权栅栏保护的启动。"""

from unittest.mock import AsyncMock, Mock

import grpc
import pytest

from collector.infrastructure.grpc.server import CollectorControlService
from core.infrastructure.rpc import collector_pb2 as pb


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "request"),
    [
        ("StartTask", pb.TaskStartRequest(task_id="task-1")),
        ("StartTaskInstance", pb.TaskInstanceStartRequest(instance_id="instance-1")),
    ],
)
async def test_unfenced_task_start_is_rejected(
    method: str, request: object
) -> None:
    """即使 CLI 直接绕过 Web Server，也不能通过 RPC 启动采集。"""
    app = Mock()
    app.runtime = Mock()
    server = CollectorControlService(app)
    context = Mock()
    context.abort = AsyncMock(side_effect=RuntimeError("aborted"))
    with pytest.raises(RuntimeError, match="aborted"):
        await getattr(server, method)(request, context)
    context.abort.assert_awaited_once_with(
        grpc.StatusCode.FAILED_PRECONDITION,
        "task ownership fencing is not configured",
    )
    app.runtime.start_task.assert_not_called()
