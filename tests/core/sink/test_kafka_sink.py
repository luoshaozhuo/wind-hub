"""KafkaSink 生命周期与写出发包契约（aiokafka 全 fake，不接触 Broker）。

- open 幂等、失败转 SinkError 且 health 转红；
- write：每测点一条 JSON、key=address.field、非有限值拒绝、发送失败记账；
- write_task 按任务白名单过滤；flush/close 语义；关闭后写拒绝。
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime

import pytest

from core.application import Quality
from core.application.sink_contract import (
    ResolvedSink,
)
from core.application.sink_error import SinkError
from core.domain.point_value import PointValue
from core.domain.sink import Sink
from core.infrastructure.sink.kafka import KafkaSink


def _point_dict(field: str = "wind_speed", device: str = "wt01", point: str = "wind_speed"):
    return {
        "source": {"device_id": device, "point_id": point},
        "ref": f"scada.{field}",
        "source_data_type": "float32",
        "source_unit": "meter_per_second",
        "datatype": "float32",
        "unit": "meter_per_second",
        "address": {"field": field},
    }


def _resolved(**overrides) -> ResolvedSink:
    values = {
        "sink_id": "kafka-1",
        "kind": "kafka",
        "enabled": True,
        "connection": {"bootstrap_servers": "127.0.0.1:9092", "topic": "telemetry"},
        "points": (_point_dict(),),
    }
    values.update(overrides)
    return ResolvedSink(**values)


class FakeProducer:
    instances: list[FakeProducer] = []
    mode = "ok"

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.sent: list[tuple[str, bytes, bytes]] = []
        FakeProducer.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")
        if self.mode == "start_fail":
            raise RuntimeError("broker unavailable")

    async def send_and_wait(self, topic: str, key: bytes, value: bytes) -> None:
        if self.mode == "send_fail":
            raise RuntimeError("not leader")
        self.sent.append((topic, key, value))

    async def flush(self) -> None:
        self.calls.append("flush")

    async def stop(self) -> None:
        self.calls.append("stop")


@pytest.fixture
def fake_aiokafka(monkeypatch: pytest.MonkeyPatch):
    FakeProducer.instances = []
    FakeProducer.mode = "ok"
    module = types.ModuleType("aiokafka")
    module.AIOKafkaProducer = FakeProducer
    monkeypatch.setitem(sys.modules, "aiokafka", module)
    return FakeProducer


def _pv(value=8.5, *, point="wind_speed", quality=Quality.GOOD) -> PointValue:
    return PointValue(
        device_id="wt01",
        point_id=point,
        value=value,
        quality=quality,
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        source="modbus",
    )


def test_requires_resolved_sink() -> None:
    plain = Sink(
        sink_id="k",
        kind="kafka",
        enabled=True,
        connection={"topic": "telemetry"},
    )
    with pytest.raises(ValueError, match="resolved output mappings"):
        KafkaSink(plain)


def test_rejects_non_stream_address() -> None:
    bad = dict(_point_dict())
    bad["address"] = {"device_id": "g", "point_id": "p"}  # GrpcSinkAddress 形状
    with pytest.raises(ValueError, match="requires StreamSinkAddress"):
        KafkaSink(_resolved(points=(bad,)))


async def test_open_is_idempotent_and_uses_delivery_guarantees(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())

    await sink.open()
    await sink.open()

    assert len(fake_aiokafka.instances) == 1
    producer = fake_aiokafka.instances[0]
    assert producer.kwargs["acks"] == "all"
    assert producer.kwargs["enable_idempotence"] is True
    assert producer.kwargs["bootstrap_servers"] == "127.0.0.1:9092"
    assert producer.calls == ["start"]

    await sink.close()


async def test_open_failure_becomes_sink_error_and_unhealthy(fake_aiokafka) -> None:
    fake_aiokafka.mode = "start_fail"
    sink = KafkaSink(_resolved())

    with pytest.raises(SinkError, match="Kafka open failed"):
        await sink.open()

    health = await sink.health()
    assert health.healthy is False
    assert "broker unavailable" in (health.message or "")
    # 失败后仍可关闭，且不残留 producer。
    await sink.close()


async def test_write_before_open_is_rejected(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    with pytest.raises(SinkError, match="not opened"):
        await sink.write([_pv()])


async def test_write_emits_one_json_message_per_point(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    await sink.open()

    await sink.write([_pv()])

    producer = fake_aiokafka.instances[0]
    assert len(producer.sent) == 1
    topic, key, raw = producer.sent[0]
    assert topic == "telemetry"
    assert key == b"wind_speed"
    payload = json.loads(raw.decode("utf-8"))
    assert payload == {
        "device_id": "wt01",
        "point_id": "wind_speed",
        "value": 8.5,
        "quality": "good",
        "timestamp": "2026-10-03T00:00:00Z",
        "source": "modbus",
    }

    await sink.close()


async def test_empty_batch_is_noop(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    await sink.open()

    await sink.write([])

    assert fake_aiokafka.instances[0].sent == []
    await sink.close()


async def test_non_finite_value_is_rejected_without_send(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    await sink.open()

    with pytest.raises(SinkError, match="non-finite"):
        await sink.write([_pv(float("nan"))])
    assert fake_aiokafka.instances[0].sent == []

    health = await sink.health()
    assert health.healthy is False
    await sink.close()


async def test_send_failure_is_wrapped_and_marked(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    await sink.open()
    fake_aiokafka.instances[0].mode = "send_fail"

    with pytest.raises(SinkError, match="Kafka write failed"):
        await sink.write([_pv()])

    health = await sink.health()
    assert health.healthy is False
    assert "not leader" in (health.message or "")
    await sink.close()


async def test_write_task_filters_by_task_whitelist(fake_aiokafka) -> None:
    resolved = _resolved(
        task_sources={"task-a": (("wt01", "wind_speed"),)},
    )
    sink = KafkaSink(resolved)
    await sink.open()

    await sink.write_task("task-a", [_pv(), _pv(1.0, point="rotor_rpm")])
    await sink.write_task("task-unknown", [_pv()])

    producer = fake_aiokafka.instances[0]
    assert len(producer.sent) == 1
    await sink.close()


async def test_flush_requires_open_and_close_stops_producer(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    with pytest.raises(SinkError, match="not opened"):
        await sink.flush()

    await sink.open()
    await sink.flush()
    assert "flush" in fake_aiokafka.instances[0].calls

    await sink.close()
    assert fake_aiokafka.instances[0].calls[-1] == "stop"
    health = await sink.health()
    assert health.healthy is False
    with pytest.raises(SinkError, match="not opened"):
        await sink.write([_pv()])


async def test_close_without_open_is_safe(fake_aiokafka) -> None:
    sink = KafkaSink(_resolved())
    await sink.close()  # 不抛异常
    assert fake_aiokafka.instances == []
