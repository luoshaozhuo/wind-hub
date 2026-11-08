"""Web Server 出站 gRPC 适配器的 protobuf 契约测试（Collector v1 + Commander v1）。

Commander 侧旧 ``PointValueMessage``/``CommandResultMessage`` 与 OUTCOME 枚举已
随 N-03 迁移退役：实时读点改走 CollectorService.ReadPoints（Sample 合同），
写控制改走 SubmitCommand/GetCommandResult。本文件只保留仍存在的生产边界：
Collector 任务/监控 DTO 转换，以及 Commander 写入标量编码 ``_encode_scalar``。
标量解码与 bytes 拒绝语义现属 Commander 服务端，由
tests/commander/integration/test_grpc.py 的标量类型用例覆盖。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import pytest

from contracts import collector_pb2 as cpb
from contracts import common_pb2
from web_server.adapter.outbound.grpc.collector import (
    CollectorGrpcClient,
    _task_instance,
    _task_summary,
)
from web_server.adapter.outbound.grpc.commander import _encode_scalar
from web_server.application.port.worker import CollectorTaskConflictError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (True, "bool_value"),
        (0, "uint64_value"),
        (-1, "sint64_value"),
        (2**63, "uint64_value"),
        (3.25, "double_value"),
        ("abc", "string_value"),
    ],
)
def test_scalar_encode_selects_correct_oneof_field(value: object, field: str) -> None:
    # 现行 _encode_scalar 以 0 为界：负数 sint64、非负数 uint64；
    # 0 的两种 varint 编码在线上等价，服务端 _scalar 对两字段均可解码。
    encoded = _encode_scalar(value)
    assert encoded.WhichOneof("kind") == field


def test_collector_task_wrapper_fields() -> None:
    message = cpb.Task(
        task_id="WT01",
        device_id="WT01",
        desired_state="START",
        actual_state="RUNNING",
        revision="rev-1",
    )
    summary = _task_summary(message)
    assert summary.device == "WT01"
    assert summary.runtime_state == "running"
    assert summary.enabled is True
    assert summary.running_instances == 1 and summary.stopped_instances == 0
    assert summary.failed_instances == 0

    stopped = cpb.Task(
        task_id="WT01",
        device_id="WT01",
        desired_state="STOP",
        actual_state="STOPPED",
        revision="rev-2",
    )
    instance = _task_instance(stopped)
    assert instance.instance_id == "WT01"
    assert instance.state == "stopped"

    faulted = cpb.Task(
        task_id="WT01",
        device_id="WT01",
        desired_state="START",
        actual_state="FAULTED",
    )
    assert _task_summary(faulted).runtime_state == "failed"
    assert _task_instance(faulted).state == "failed"


def _v1_client() -> CollectorGrpcClient:
    client = object.__new__(CollectorGrpcClient)
    client.default_timeout = 5.0
    client._collector = SimpleNamespace()
    client._common = SimpleNamespace()
    return client


@pytest.mark.asyncio
async def test_collector_status_and_metrics_dtos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = _v1_client()
    client._common.GetServiceStatus = AsyncMock(
        return_value=common_pb2.GetServiceStatusResponse(health="HEALTHY")
    )
    client._collector.ListDevices = AsyncMock(
        return_value=cpb.ListDevicesResponse(
            devices=[
                cpb.Device(device_id="dev-1", actual_state="RUNNING"),
                cpb.Device(device_id="dev-2", actual_state="STOPPED"),
            ]
        )
    )
    client._common.GetMetrics = AsyncMock(
        return_value=common_pb2.GetMetricsResponse(
            metrics=[
                common_pb2.Metric(name="collector.points.collected", value=12),
                common_pb2.Metric(name="collector.publisher.published", value=11),
                common_pb2.Metric(name="collector.publisher.dropped", value=1),
            ]
        )
    )

    status = await client.runtime_status()
    assert status.running is True
    assert status.device_count == 2
    assert status.devices_connected == 1
    assert status.points_collected == 12
    assert status.points_routed == 11
    assert status.points_dropped == 1

    client._common.GetEvents = AsyncMock(return_value=common_pb2.GetEventsResponse())
    metrics = await client.metrics_snapshot()
    assert metrics.counters.points_total == 12


@pytest.mark.asyncio
async def test_collector_monitoring_event_timestamp_is_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = _v1_client()
    event = common_pb2.Event(
        severity="warning",
        source="acquisition",
        message="x",
    )
    event.timestamp.FromDatetime(datetime(2026, 10, 15, tzinfo=UTC))
    client._common.GetMetrics = AsyncMock(return_value=common_pb2.GetMetricsResponse())
    client._common.GetEvents = AsyncMock(return_value=common_pb2.GetEventsResponse(events=[event]))

    dto = await client.metrics_snapshot()
    assert dto.events[0].timestamp == datetime(2026, 10, 15, tzinfo=UTC)
    assert dto.events[0].kind == "warning"
    assert dto.events[0].object == "acquisition"


@pytest.mark.asyncio
async def test_metrics_events_omitted_without_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无管理员令牌时不得拉取事件流，也不得伪造观测事件。"""
    monkeypatch.delenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", raising=False)
    client = _v1_client()
    client._common.GetMetrics = AsyncMock(return_value=common_pb2.GetMetricsResponse())
    client._common.GetEvents = AsyncMock(side_effect=AssertionError("events require admin token"))

    dto = await client.metrics_snapshot()
    assert dto.events == []
    client._common.GetEvents.assert_not_called()


@pytest.mark.asyncio
async def test_device_start_uses_revision_and_admin_metadata(monkeypatch) -> None:
    """v1 设备级 START：先 GetTask 取 task.revision，再带管理员元数据下发 OperationRequest。"""
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = _v1_client()
    start_mock = AsyncMock(
        return_value=common_pb2.OperationResult(accepted=True, actual_state="RUNNING")
    )
    client._collector.GetTask = AsyncMock(
        side_effect=[
            cpb.GetTaskResponse(task=cpb.Task(task_id="WT01", device_id="WT01", revision="rev-17")),
            cpb.GetTaskResponse(
                task=cpb.Task(
                    task_id="WT01",
                    device_id="WT01",
                    revision="rev-18",
                    desired_state="START",
                    actual_state="RUNNING",
                )
            ),
        ]
    )
    client._collector.StartTask = start_mock

    summary = await client.start_task("WT01")
    assert summary.device == "WT01" and summary.runtime_state == "running"
    request = start_mock.call_args.args[0]
    assert request.id == "WT01"
    assert request.expected_revision == "rev-17"
    metadata = dict(start_mock.call_args.kwargs["metadata"])
    assert metadata["x-windhub-admin-token"] == "web-token"


@pytest.mark.asyncio
async def test_start_without_admin_token_raises(monkeypatch) -> None:
    monkeypatch.delenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", raising=False)
    client = _v1_client()
    client._collector.GetTask = AsyncMock(
        return_value=cpb.GetTaskResponse(
            task=cpb.Task(task_id="WT01", device_id="WT01", revision="rev-1")
        )
    )
    client._collector.StartTask = AsyncMock()
    with pytest.raises(PermissionError):
        await client.start_task("WT01")
    client._collector.StartTask.assert_not_called()


@pytest.mark.asyncio
async def test_revision_conflict_mapped_to_task_conflict(monkeypatch) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = _v1_client()
    client._collector.GetTask = AsyncMock(
        return_value=cpb.GetTaskResponse(
            task=cpb.Task(task_id="WT01", device_id="WT01", revision="rev-1")
        )
    )

    async def _abort(*args, **kwargs):
        raise grpc.aio.AioRpcError(
            grpc.StatusCode.ABORTED,
            SimpleNamespace(),
            SimpleNamespace(),
            "configuration revision conflict",
        )

    client._collector.StartTask = _abort
    with pytest.raises(CollectorTaskConflictError):
        await client.start_task("WT01")


@pytest.mark.asyncio
async def test_rejected_operation_result_maps_to_conflict(monkeypatch) -> None:
    """服务端接受 RPC 但 OperationResult.accepted=False 时同样映射为冲突，不伪造成功。"""
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = _v1_client()
    client._collector.GetTask = AsyncMock(
        return_value=cpb.GetTaskResponse(
            task=cpb.Task(task_id="WT01", device_id="WT01", revision="rev-1")
        )
    )
    client._collector.StartTask = AsyncMock(
        return_value=common_pb2.OperationResult(
            accepted=False, error_code="REVISION_CONFLICT", message="stale revision"
        )
    )
    with pytest.raises(CollectorTaskConflictError, match="stale revision"):
        await client.start_task("WT01")


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
def test_scalar_out_of_wire_range_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="64-bit"):
        _encode_scalar(value)
