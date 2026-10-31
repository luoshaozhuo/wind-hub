"""Web Server 到当前 Collector v2 / Commander 的 protobuf 契约测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import pytest

from contracts import collector_v2_pb2 as cpb
from contracts import commander_pb2, common_pb2
from core.application.protocol_contract import WriteOutcome
from web_server.adapter.outbound.grpc.collector import (
    CollectorGrpcClient,
    _task_instance,
    _task_summary,
)
from web_server.adapter.outbound.grpc.commander import (
    _command_result,
    _decode_scalar,
    _encode_scalar,
    _point_value,
)
from web_server.application.port.worker import CollectorTaskConflictError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (True, "bool_value"),
        (0, "sint64_value"),
        (-1, "sint64_value"),
        (2**63, "uint64_value"),
        (3.25, "double_value"),
        ("abc", "string_value"),
    ],
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
    message = cpb.TaskStatus(
        task_id="WT01",
        device_id="WT01",
        actual_state=cpb.RUNNING,
    )
    message.interval_ms = 2500
    summary = _task_summary(message)
    assert summary.device == "WT01"
    assert summary.interval == 2.5
    assert summary.runtime_state == "running"
    assert summary.running_instances == 1 and summary.stopped_instances == 0

    stopped = cpb.TaskStatus(task_id="WT01", device_id="WT01", actual_state=cpb.STOPPED)
    instance = _task_instance(stopped)
    assert instance.instance_id == "WT01"
    assert instance.state == "stopped"


def test_collector_status_and_metrics_dtos() -> None:
    client = object.__new__(CollectorGrpcClient)
    client.default_timeout = 5.0
    client._runtime = SimpleNamespace(
        GetRuntimeStatus=AsyncMock(
            return_value=cpb.RuntimeStatusResponse(running=True, device_total=2, device_connected=1)
        )
    )
    client._metrics = SimpleNamespace(
        GetMetrics=AsyncMock(
            return_value=cpb.GetMetricsResponse(
                metrics=[
                    cpb.MetricValue(name="points_total", value=12, valid=True),
                    cpb.MetricValue(name="device_reconnects", device_id="dev", value=3, valid=True),
                ]
            )
        ),
        GetMetricsEvents=AsyncMock(return_value=cpb.GetMetricsEventsResponse()),
    )

    import asyncio

    status = asyncio.get_event_loop().run_until_complete(client.runtime_status())
    assert status.running is True
    assert status.device_count == 2
    assert status.devices_connected == 1
    assert status.points_collected == 12

    metrics = asyncio.get_event_loop().run_until_complete(client.metrics_snapshot())
    assert metrics.counters.points_total == 12
    assert metrics.device_reconnects == {"dev": 3}


def test_collector_monitoring_event_timestamp_is_utc() -> None:
    client = object.__new__(CollectorGrpcClient)
    client.default_timeout = 5.0
    client._runtime = SimpleNamespace()
    event = cpb.MetricEvent(event_type="acquisition", message="x")
    event.timestamp.FromDatetime(datetime(2026, 10, 15, tzinfo=UTC))
    client._metrics = SimpleNamespace(
        GetMetrics=AsyncMock(return_value=cpb.GetMetricsResponse()),
        GetMetricsEvents=AsyncMock(return_value=cpb.GetMetricsEventsResponse(events=[event])),
    )

    import asyncio

    dto = asyncio.get_event_loop().run_until_complete(client.metrics_snapshot())
    assert dto.events[0].timestamp == datetime(2026, 10, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_device_start_uses_revision_and_admin_metadata(monkeypatch) -> None:
    """v2 设备级 START：先取 devices stored_revision，再带管理员元数据下发。"""
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = object.__new__(CollectorGrpcClient)
    client.default_timeout = 5.0
    start_mock = AsyncMock(return_value=cpb.TaskChangeResponse())
    get_task_mock = AsyncMock(
        return_value=cpb.TaskStatus(task_id="WT01", device_id="WT01", actual_state=cpb.RUNNING)
    )
    config_status = cpb.GetConfigStatusResponse(
        configs=[
            cpb.ConfigApplyStatus(
                selector=cpb.ConfigSelector(scope=cpb.INSTANCE, name="devices"),
                stored_revision="rev-17",
            )
        ]
    )
    client._config = SimpleNamespace(GetConfigStatus=AsyncMock(return_value=config_status))
    client._tasks = SimpleNamespace(
        StartTask=start_mock,
        StopTask=AsyncMock(return_value=cpb.TaskChangeResponse()),
        GetTask=get_task_mock,
    )

    summary = await client.start_task("WT01")
    assert summary.device == "WT01" and summary.runtime_state == "running"
    request = start_mock.call_args.args[0]
    assert request.device_id == "WT01"
    assert request.expected_revision == "rev-17"
    assert request.request_id
    metadata = dict(start_mock.call_args.kwargs["metadata"])
    assert metadata["x-windhub-admin-token"] == "web-token"


@pytest.mark.asyncio
async def test_start_without_admin_token_raises(monkeypatch) -> None:
    monkeypatch.delenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", raising=False)
    client = object.__new__(CollectorGrpcClient)
    with pytest.raises(PermissionError):
        await client.start_task("WT01")


@pytest.mark.asyncio
async def test_revision_conflict_mapped_to_task_conflict(monkeypatch) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "web-token")
    client = object.__new__(CollectorGrpcClient)
    client.default_timeout = 5.0
    client._config = SimpleNamespace(
        GetConfigStatus=AsyncMock(
            return_value=cpb.GetConfigStatusResponse(
                configs=[
                    cpb.ConfigApplyStatus(
                        selector=cpb.ConfigSelector(scope=cpb.INSTANCE, name="devices"),
                        stored_revision="rev-1",
                    )
                ]
            )
        )
    )

    async def _abort(*args, **kwargs):
        raise grpc.aio.AioRpcError(
            grpc.StatusCode.ABORTED,
            SimpleNamespace(),
            SimpleNamespace(),
            "configuration revision conflict",
        )

    client._tasks = SimpleNamespace(StartTask=_abort, StopTask=AsyncMock())
    with pytest.raises(CollectorTaskConflictError):
        await client.start_task("WT01")


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
def test_scalar_out_of_wire_range_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="64-bit"):
        _encode_scalar(value)


@pytest.mark.parametrize(
    "outcome",
    [
        commander_pb2.COMMAND_OUTCOME_CONFIRMED_SUCCESS,
        commander_pb2.COMMAND_OUTCOME_CONFIRMED_FAILURE,
        commander_pb2.COMMAND_OUTCOME_UNKNOWN,
    ],
)
def test_command_result_outcomes(outcome: int) -> None:
    message = commander_pb2.CommandResultMessage(command_id="c", outcome=outcome)
    message.finished_at.GetCurrentTime()
    result = _command_result(message)
    assert result.command_id == "c"
    assert result.finished_at.tzinfo is not None


def test_command_result_missing_timestamp_is_rejected() -> None:
    message = commander_pb2.CommandResultMessage(
        command_id="c",
        outcome=commander_pb2.COMMAND_OUTCOME_CONFIRMED_SUCCESS,
    )
    with pytest.raises(ValueError, match="finish timestamp"):
        _command_result(message)


def test_read_value_missing_timestamp_is_rejected() -> None:
    message = commander_pb2.PointValueMessage(device_id="d", point_id="p", quality="good")
    with pytest.raises(ValueError, match="timestamp"):
        _point_value(message)


def test_bytes_scalar_is_not_misrepresented_as_business_value() -> None:
    scalar = common_pb2.ScalarValue(bytes_value=b"abc")
    with pytest.raises(ValueError, match="byte-valued"):
        _decode_scalar(scalar)


def test_empty_commander_scalar_rejected() -> None:
    with pytest.raises(ValueError, match="no active value"):
        _decode_scalar(common_pb2.ScalarValue())


def test_point_value_without_scalar_rejected() -> None:
    message = commander_pb2.PointValueMessage(device_id="d", point_id="p", quality="good")
    message.timestamp.GetCurrentTime()
    with pytest.raises(ValueError, match="no scalar"):
        _point_value(message)


def test_command_result_without_id_rejected() -> None:
    message = commander_pb2.CommandResultMessage(
        outcome=commander_pb2.COMMAND_OUTCOME_CONFIRMED_SUCCESS
    )
    message.finished_at.GetCurrentTime()
    with pytest.raises(ValueError, match="command ID"):
        _command_result(message)


def test_invalid_point_quality_rejected() -> None:
    message = commander_pb2.PointValueMessage(device_id="d", point_id="p", quality="not-a-quality")
    message.timestamp.GetCurrentTime()
    message.value.double_value = 1.0
    with pytest.raises(ValueError):
        _point_value(message)
