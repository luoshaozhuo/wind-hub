"""GrpcSink 单元契约（channel/stub 全 fake，不监听真实端口）。

- 构造：必须 ResolvedSink、地址必须 GrpcSinkAddress、源映射去重；
- 写：仅 GOOD 非空、已映射值外发；批内同一目的点重复 → ValueError；
  关闭后写 → RuntimeError；
- health：未开/就绪/超时/RPC 错误四态；
- write_task 任务白名单过滤。
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from contracts import gateway_pb2 as pb
from core.application import ConnectionHealth, Quality
from core.application.sink_contract import (
    ResolvedSink,
)
from core.domain.point_value import PointValue
from core.infrastructure.sink import grpc_sink as module
from core.infrastructure.sink.grpc_sink import GrpcSink


def _point_dict(
    *,
    source_device: str = "wt01",
    source_point: str = "wind_speed",
    target_device: str = "gw01",
    target_point: str = "wind_speed",
):
    return {
        "source": {"device_id": source_device, "point_id": source_point},
        "ref": f"scada.{target_point}",
        "source_data_type": "float32",
        "source_unit": "meter_per_second",
        "datatype": "float32",
        "unit": "meter_per_second",
        "address": {"device_id": target_device, "point_id": target_point},
    }


def _resolved(**overrides) -> ResolvedSink:
    values = {
        "sink_id": "grpc-1",
        "kind": "grpc",
        "enabled": True,
        "connection": {"endpoint": "127.0.0.1:50073", "timeout": 0.5},
        "points": (_point_dict(),),
    }
    values.update(overrides)
    return ResolvedSink(**values)


def _pv(
    value=8.5,
    *,
    device="wt01",
    point="wind_speed",
    quality=Quality.GOOD,
) -> PointValue:
    return PointValue(device_id=device, point_id=point, value=value, quality=quality)


class FakeStub:
    def __init__(self, channel) -> None:
        self.channel = channel
        self.requests: list[pb.WriteManyRequest] = []

    async def WriteMany(self, request: pb.WriteManyRequest, *, timeout: float):  # noqa: N802 - gRPC stub 原名
        if self.channel.mode == "rpc_error":
            raise FakeRpcError("UNAVAILABLE")
        self.requests.append(request)
        self.last_timeout = timeout
        return object()


class FakeRpcError(grpc.RpcError):
    pass


class FakeChannel:
    mode = "ready"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.closed = False
        self.ready_calls = 0

    def channel_ready(self):
        self.ready_calls += 1
        if self.mode == "timeout":

            async def _timeout():
                await asyncio.sleep(5)

            return asyncio.wait_for(_timeout(), timeout=0.05)
        if self.mode == "rpc_error":

            async def _error():
                raise FakeRpcError("UNAVAILABLE")

            return _error()

        async def _ready():
            return None

        return _ready()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_grpc(monkeypatch: pytest.MonkeyPatch):
    FakeChannel.mode = "ready"
    channels: list[FakeChannel] = []

    def fake_channel(endpoint: str) -> FakeChannel:
        channel = FakeChannel(endpoint)
        channels.append(channel)
        return channel

    monkeypatch.setattr(grpc.aio, "insecure_channel", fake_channel)
    monkeypatch.setattr(module.rpc, "GatewayDataServiceStub", FakeStub)
    return channels


def test_rejects_plain_domain_sink() -> None:
    from core.domain import Sink

    plain = Sink(
        sink_id="g",
        kind="grpc",
        enabled=True,
        connection={"endpoint": "127.0.0.1:50073"},
    )
    with pytest.raises(ValueError, match="resolved Collector mappings"):
        GrpcSink(plain)


def test_rejects_non_grpc_address() -> None:
    bad = dict(_point_dict())
    bad["address"] = {"field": "wind_speed"}  # StreamSinkAddress
    with pytest.raises(ValueError, match="requires GrpcSinkAddress"):
        GrpcSink(_resolved(points=(bad,)))


def test_rejects_duplicate_source_mapping() -> None:
    point = _point_dict()
    with pytest.raises(ValueError, match="duplicate gRPC source mapping"):
        GrpcSink(_resolved(points=(point, point)))


async def test_open_is_idempotent_and_close_clears(fake_grpc) -> None:
    sink = GrpcSink(_resolved())

    await sink.open()
    await sink.open()
    assert len(fake_grpc) == 1
    assert isinstance(sink._write_many, FakeStub)

    await sink.close()
    assert fake_grpc[0].closed
    assert sink._channel is None
    assert sink._write_many is None


async def test_close_without_open_is_safe(fake_grpc) -> None:
    sink = GrpcSink(_resolved())
    await sink.close()
    assert fake_grpc == []


async def test_write_forwards_good_mapped_scalar(fake_grpc) -> None:
    sink = GrpcSink(_resolved())
    await sink.open()

    await sink.write([_pv(7.5)])

    stub = sink._write_many
    assert isinstance(stub, FakeStub)
    request = stub.requests[0]
    assert len(request.points) == 1
    point = request.points[0]
    assert point.device_id == "gw01"
    assert point.point_id == "wind_speed"
    assert point.value.double_value == pytest.approx(7.5)
    assert stub.last_timeout == 0.5

    await sink.close()


async def test_write_filters_bad_quality_none_and_unmapped(fake_grpc) -> None:
    sink = GrpcSink(_resolved())
    await sink.open()

    await sink.write(
        [
            _pv(1.0, quality=Quality.BAD),
            _pv(None),
            _pv(1.0, device="other", point="unmapped"),
        ]
    )

    stub = sink._write_many
    assert isinstance(stub, FakeStub)
    assert stub.requests == []

    await sink.close()


async def test_empty_batch_is_noop(fake_grpc) -> None:
    sink = GrpcSink(_resolved())
    await sink.open()

    await sink.write([])

    stub = sink._write_many
    assert isinstance(stub, FakeStub)
    assert stub.requests == []
    await sink.close()


async def test_write_on_closed_sink_raises(fake_grpc) -> None:
    sink = GrpcSink(_resolved())
    with pytest.raises(RuntimeError, match="closed"):
        await sink.write([_pv()])


async def test_duplicate_feedback_target_in_batch_raises(fake_grpc) -> None:
    points = (
        _point_dict(source_device="wt01", target_device="gw01", target_point="same"),
        _point_dict(source_device="wt02", target_device="gw01", target_point="same"),
    )
    sink = GrpcSink(_resolved(points=points))
    await sink.open()

    with pytest.raises(ValueError, match="duplicate feedback point"):
        await sink.write([_pv(device="wt01"), _pv(device="wt02")])

    await sink.close()


async def test_write_task_filters_by_task_whitelist(fake_grpc) -> None:
    sink = GrpcSink(_resolved(task_sources={"task-a": (("wt01", "wind_speed"),)}))
    await sink.open()

    await sink.write_task("task-a", [_pv()])
    await sink.write_task("task-unknown", [_pv()])

    stub = sink._write_many
    assert isinstance(stub, FakeStub)
    assert len(stub.requests) == 1
    await sink.close()


async def test_health_states(fake_grpc) -> None:
    sink = GrpcSink(_resolved())

    closed_health = await sink.health()
    assert closed_health == ConnectionHealth(healthy=False, message="gRPC channel closed")

    await sink.open()
    assert (await sink.health()).healthy is True

    fake_grpc[0].mode = "timeout"
    health = await sink.health()
    assert health.healthy is False

    fake_grpc[0].mode = "rpc_error"
    health = await sink.health()
    assert health.healthy is False
    assert "UNAVAILABLE" in (health.message or "")

    await sink.close()
