"""CTL 直连 Collector / Commander gRPC 控制面的组件测试。"""

from __future__ import annotations

import grpc
import pytest

from core.infrastructure.rpc import collector_pb2 as collector_pb
from core.infrastructure.rpc import collector_pb2_grpc as collector_grpc
from core.infrastructure.rpc import commander_pb2 as commander_pb
from core.infrastructure.rpc import commander_pb2_grpc as commander_grpc
from wind_hub_cli.client import CollectorClient, CommanderClient


class _CollectorRuntime(collector_grpc.CollectorRuntimeServiceServicer):
    async def GetRuntimeStatus(self, request, context):  # noqa: N802
        """返回最小 Runtime 状态。"""
        del request, context
        return collector_pb.RuntimeStatusResponse(
            running=True,
            device_count=2,
            devices_connected=1,
        )


class _CollectorControl(collector_grpc.CollectorControlServiceServicer):
    async def StopTask(self, request, context):  # noqa: N802
        """回显 stop-task。"""
        del context
        return collector_pb.TaskSummaryMessage(
            task_id=request.task_id,
            runtime_state="stopped",
        )


class _Commander(commander_grpc.CommanderServiceServicer):
    async def GetStatus(self, request, context):  # noqa: N802
        """返回最小 Commander 状态。"""
        del request, context
        return commander_pb.CommanderStatusResponse(
            running=True,
            device_count=1,
        )

    async def ReadPoint(self, request, context):  # noqa: N802
        """回显一个读取值。"""
        del context
        return commander_pb.PointValueMessage(
            device_id=request.device_id,
            point_id=request.point_id,
            value=commander_pb.ScalarValue(double_value=12.5),
            quality="good",
        )

    async def WritePoint(self, request, context):  # noqa: N802
        """确认一个写命令。"""
        del context
        return commander_pb.CommandResultMessage(
            command_id=request.command_id,
            success=True,
        )


@pytest.fixture
async def endpoints():
    """启动两个最小真实 gRPC Server。"""
    collector_server = grpc.aio.server()
    # 生成的 *_pb2_grpc.pyi 将全部 RPC 标为 abstractmethod，但运行时 .py
    # Servicer 是普通类；fake 只需实现被测 RPC，故忽略 [abstract]。
    collector_grpc.add_CollectorRuntimeServiceServicer_to_server(
        _CollectorRuntime(),  # type: ignore[abstract]
        collector_server,
    )
    collector_grpc.add_CollectorControlServiceServicer_to_server(
        _CollectorControl(),  # type: ignore[abstract]
        collector_server,
    )
    collector_port = collector_server.add_insecure_port("127.0.0.1:0")

    commander_server = grpc.aio.server()
    commander_grpc.add_CommanderServiceServicer_to_server(
        _Commander(),  # type: ignore[abstract]
        commander_server,
    )
    commander_port = commander_server.add_insecure_port("127.0.0.1:0")

    await collector_server.start()
    await commander_server.start()
    try:
        yield (
            f"127.0.0.1:{collector_port}",
            f"127.0.0.1:{commander_port}",
        )
    finally:
        await collector_server.stop(0)
        await commander_server.stop(0)


async def test_collector_query_and_control_without_server_admin(endpoints) -> None:
    """Collector 查询与任务控制均直接走 Collector gRPC。"""
    collector_target, _ = endpoints

    async with CollectorClient(collector_target, timeout=1.0) as client:
        status = await client.call("status")
        stopped = await client.call("stop-task", task_id="task-1")

    assert status["running"] is True
    assert status["device_count"] == 2
    assert stopped["task_id"] == "task-1"
    assert stopped["runtime_state"] == "stopped"


async def test_commander_read_write_without_server_admin(endpoints) -> None:
    """Commander 即时读写均直接走 Commander gRPC。"""
    _, commander_target = endpoints

    async with CommanderClient(commander_target, timeout=1.0) as client:
        status = await client.call("status")
        reading = await client.call("read", device_id="dev1", point_id="p1")
        result = await client.call(
            "write",
            command_id="cmd-1",
            device_id="dev1",
            point_id="p1",
            value=13.5,
            io_timeout=1.0,
        )

    assert status["running"] is True
    assert reading["value"]["double_value"] == 12.5
    assert reading["quality"] == "good"
    assert result["success"] is True
