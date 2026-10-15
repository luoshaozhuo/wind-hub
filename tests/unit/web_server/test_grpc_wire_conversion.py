"""Web Server 到当前 Collector/Commander 的 protobuf 契约测试。"""

from datetime import UTC, datetime

import pytest

from core.application.protocol_contract import WriteOutcome
from core.infrastructure.rpc import collector_pb2, commander_pb2
from web_server.adapter.outbound.grpc.collector import (
    _metrics_snapshot_dto,
    _runtime_status_dto,
    _task_instance,
    _task_summary,
)
from web_server.adapter.outbound.grpc.commander import (
    _command_result,
    _decode_scalar,
    _encode_scalar,
    _point_value,
)


@pytest.mark.parametrize(
    ("value", "field"),
    [(True, "bool_value"), (0, "sint64_value"), (-1, "sint64_value"),
     (2**63, "uint64_value"), (3.25, "double_value"), ("abc", "string_value")],
)
def test_scalar_wire_round_trip(value: object, field: str) -> None:
    encoded = _encode_scalar(value)
    assert encoded.WhichOneof("kind") == field
    assert _decode_scalar(encoded) == value


def test_write_outcome_preserves_unknown() -> None:
    message = commander_pb2.CommandResultMessage(
        command_id="cmd-1",
        outcome=commander_pb2.COMMAND_OUTCOME_UNKNOWN,
        error="timeout",
    )
    message.finished_at.GetCurrentTime()
    result = _command_result(message)
    assert result.outcome is WriteOutcome.UNKNOWN
    assert result.error == "timeout"
    assert result.finished_at.tzinfo is not None


def test_invalid_write_outcome_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported command outcome"):
        _command_result(commander_pb2.CommandResultMessage())


def test_read_value_uses_utc_timestamp() -> None:
    message = commander_pb2.PointValueMessage(
        device_id="dev", point_id="p", quality="good", timestamp_source="device"
    )
    message.timestamp.FromDatetime(datetime(2026, 10, 15, tzinfo=UTC))
    message.value.double_value = 4.5
    sample = _point_value(message)
    assert sample.value == 4.5
    assert sample.timestamp.tzinfo is not None
    assert sample.timestamp_source == "device"


def test_collector_task_wrapper_fields() -> None:
    message = collector_pb2.TaskSummaryMessage(task_id="task", point_group="all")
    message.device_group.value = "group"
    message.interval.value = 2.5
    message.targets.append("sink")
    summary = _task_summary(message)
    assert summary.device_group == "group"
    assert summary.interval == 2.5
    assert summary.targets == ["sink"]
    assert summary.device is None

    instance = collector_pb2.TaskInstanceMessage(instance_id="i", task_id="task")
    instance.targets.append("sink")
    assert _task_instance(instance).targets == ["sink"]


def test_collector_status_and_metrics() -> None:
    status = collector_pb2.RuntimeStatusResponse(running=True, points_collected=12)
    acquisition = status.acquisitions.add()
    acquisition.instance_id = "instance"
    acquisition.last_duration.value = 0.2
    converted = _runtime_status_dto(status)
    assert converted.points_collected == 12
    assert converted.acquisitions[0].last_duration == 0.2

    metrics = collector_pb2.MetricsSnapshotResponse()
    metrics.counters.points_total = 13
    counter = metrics.device_reconnects.add()
    counter.key = "dev"
    counter.value = 3
    dto = _metrics_snapshot_dto(metrics)
    assert dto.counters.points_total == 13
    assert dto.device_reconnects == {"dev": 3}


@pytest.mark.asyncio
async def test_collector_unsafe_task_operations_do_not_call_rpc() -> None:
    """当前 wire 没有所有权栅栏；不可绕过 Server 直接启动任务。"""
    from web_server.adapter.outbound.grpc.collector import CollectorGrpcClient
    from web_server.application.port.worker import CollectorPlacementRejectedError

    client = object.__new__(CollectorGrpcClient)
    with pytest.raises(CollectorPlacementRejectedError, match="ApplyTaskPlacement"):
        await client.apply_task_placement("collector-1", 1, ["task-1"])
    with pytest.raises(CollectorPlacementRejectedError, match="ownership fencing"):
        await client.start_task("task-1", 1)
    with pytest.raises(CollectorPlacementRejectedError, match="ownership fencing"):
        await client.start_task_instance("instance-1", 1)


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
def test_scalar_out_of_wire_range_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="64-bit"):
        _encode_scalar(value)


def test_collector_monitoring_event_timestamp_is_utc() -> None:
    metrics = collector_pb2.MetricsSnapshotResponse()
    event = metrics.events.add()
    event.timestamp.FromDatetime(datetime(2026, 10, 15, tzinfo=UTC))
    event.kind = "acquisition"
    dto = _metrics_snapshot_dto(metrics)
    assert dto.events[0].timestamp == datetime(2026, 10, 15, tzinfo=UTC)
