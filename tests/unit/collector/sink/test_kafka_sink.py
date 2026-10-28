"""KafkaSink 配置装配、任务过滤、确认发送和资源生命周期测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from collector.infrastructure.sink_registry import build_sink_registry
from core.application.sink_contract import ResolvedSink
from core.application.sink_error import SinkError
from core.domain.point_value import PointValue
from core.infrastructure.sink.kafka import KafkaSink


class _Producer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes, bytes]] = []
        self.flushed = False
        self.stopped = False

    async def send_and_wait(self, topic: str, *, key: bytes, value: bytes) -> None:
        self.messages.append((topic, key, value))

    async def flush(self) -> None:
        self.flushed = True

    async def stop(self) -> None:
        self.stopped = True


def _config() -> ResolvedSink:
    return ResolvedSink(
        sink_id="kafka",
        kind="kafka",
        enabled=True,
        connection={"topic": "telemetry"},
        points=(
            {
                "source": {"device_id": "wtg-1", "point_id": "power"},
                "ref": "active_power",
                "source_data_type": "float32",
                "source_unit": "kW",
                "datatype": "float32",
                "unit": "kW",
                "address": {"field": "plant/wtg-1/power"},
            },
        ),
        task_sources={"task-a": (("wtg-1", "power"),)},
    )


def test_kafka_registered_and_requires_resolved_mapping() -> None:
    sink = build_sink_registry().create(_config())
    assert isinstance(sink, KafkaSink)


@pytest.mark.asyncio
async def test_kafka_write_task_filters_and_sends_confirmed_json() -> None:
    sink = KafkaSink(_config())
    producer = _Producer()
    sink._producer = producer
    sink._error = None
    try:
        await sink.write_task(
            "task-a",
            [PointValue("wtg-1", "power", 12.5), PointValue("wtg-2", "power", 20.0)],
        )
        assert len(producer.messages) == 1
        topic, key, value = producer.messages[0]
        assert topic == "telemetry"
        assert key == b"plant/wtg-1/power"
        payload = json.loads(value)
        assert payload["device_id"] == "wtg-1"
        assert payload["point_id"] == "power"
        assert payload["value"] == 12.5
        assert payload["quality"] == "good"
        assert payload["timestamp"]
        assert (await sink.health()).healthy
        await sink.flush()
        assert producer.flushed
    finally:
        await sink.close()
    assert producer.stopped
    assert not (await sink.health()).healthy


@pytest.mark.asyncio
async def test_kafka_unopened_write_fails_without_silent_drop() -> None:
    sink = KafkaSink(_config())
    with pytest.raises(SinkError, match="not opened"):
        await sink.write([PointValue("wtg-1", "power", 10.0)])
    with pytest.raises(SinkError, match="not opened"):
        await sink.flush()


@pytest.mark.asyncio
async def test_kafka_send_failure_reports_partial_delivery() -> None:
    sink = KafkaSink(_config())

    class _FailingProducer(_Producer):
        async def send_and_wait(self, topic: str, *, key: bytes, value: bytes) -> None:
            raise RuntimeError("broker unavailable")

    sink._producer = _FailingProducer()
    sink._error = None
    try:
        with pytest.raises(SinkError, match="some points may be delivered"):
            await sink.write([PointValue("wtg-1", "power", 10.0)])
        assert not (await sink.health()).healthy
    finally:
        await sink.close()
