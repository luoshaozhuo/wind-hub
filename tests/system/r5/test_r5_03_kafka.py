"""R5-3：NATS Core → Sink Service → Kafka 真实生产/消费链路。

验证侧使用独立 ``AIOKafkaConsumer`` 直连同一 broker 读消息，绝不以 Sink
管理面状态代替外部可观察结果。覆盖：topic/key/value 与配置一致、生产者
身份、多 Task/点过滤、Broker 断连期间失败不假成功且恢复后续写、管理面
Stop/Start/Restart。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.support.process import free_port
from tests.support.wait import wait_until
from tests.system.r5.conftest import make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import DEVICE_ID, TASK_ID, write_r5_config

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


def _kafka_sink(*, topic: str, bootstrap: str, points: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "kafka_sink": {
            "type": "kafka",
            "connection": {
                "bootstrap_servers": bootstrap,
                "topic": topic,
                "client_id": f"r5-producer-{uuid.uuid4().hex[:8]}",
                "timeout": 10.0,
            },
            "outputs": {TASK_ID: {"points": points}},
        }
    }


def _mapping(point: str, field: str) -> dict[str, str]:
    return {
        "source": {"device_id": DEVICE_ID, "point_id": point},
        "address": {"field": field},
    }


async def _drain_topic(
    bootstrap: str, topic: str, *, timeout: float = 20.0, seek_earliest: bool = True
) -> list[dict[str, Any]]:
    """独立真实 consumer：返回窗口内收到的全部消息（key/value 解析后）。"""
    from aiokafka import AIOKafkaConsumer
    from aiokafka.structs import TopicPartition

    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        group_id=f"r5-verify-{uuid.uuid4().hex[:12]}",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        consumer_timeout_ms=2000,
    )
    await consumer.start()
    try:
        consumer.subscribe([topic])
        # 等待分区分配；新 topic 由生产端 auto.create 后再追订一次。
        assigned: set[TopicPartition] = set()

        async def _assigned() -> bool | None:
            assigned.update(consumer.assignment())
            return True if assigned else None

        try:
            await wait_until(
                _assigned, timeout=15.0, interval=0.2, description="partition assigned"
            )
        except Exception:
            return []
        if seek_earliest:
            await consumer.seek_to_beginning(*assigned)
        messages: list[dict[str, Any]] = []
        deadline_loop = timeout
        import asyncio

        end = asyncio.get_event_loop().time() + deadline_loop
        while asyncio.get_event_loop().time() < end:
            batch = await consumer.getmany(timeout_ms=500, max_records=100)
            for _, records in batch.items():
                for record in records:
                    messages.append({"key": record.key.decode(), "value": json.loads(record.value)})
        return messages
    finally:
        await consumer.stop()


async def test_topic_key_value_and_identity(r5_sink_factory, r5_stack, tmp_path: Path) -> None:
    topic = f"r5-topic-{uuid.uuid4().hex[:8]}"
    field_speed = f"wh.r5.{DEVICE_ID}.rotor_speed"
    field_power = f"wh.r5.{DEVICE_ID}.gen_power"
    sinks = _kafka_sink(
        topic=topic,
        bootstrap=r5_stack.kafka,
        points=[
            _mapping("rotor_speed", field_speed),
            _mapping("gen_power", field_power),
        ],
    )
    config_dir = write_r5_config(tmp_path / "cfg", free_port(), r5_stack.nats, sinks)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("kafka_sink",))
    await wait_state(sink, "kafka_sink", "running")

    await publish(r5_stack, make_sample("rotor_speed", 12.5, sample_id="r5k-1"), "realtime")
    await publish(r5_stack, make_sample("gen_power", 300.0, sample_id="r5k-2"), "realtime")

    async def _two() -> list[dict[str, Any]] | None:
        messages = await _drain_topic(r5_stack.kafka, topic, timeout=6.0)
        by_point = {m["value"]["point_id"]: m for m in messages}
        if {"rotor_speed", "gen_power"}.issubset(by_point):
            return list(by_point.values())
        return None

    records = await wait_until(_two, timeout=40.0, interval=2.0, description="both points consumed")
    by_point = {m["value"]["point_id"]: m for m in records}
    speed = by_point["rotor_speed"]
    assert speed["key"] == field_speed
    value = speed["value"]
    assert value["device_id"] == DEVICE_ID
    assert value["point_id"] == "rotor_speed"
    assert value["value"] == pytest.approx(12.5)
    assert value["quality"] == "good"
    assert (
        isinstance(value["timestamp"], str)
        and value["timestamp"].endswith("+00:00")
        or (isinstance(value["timestamp"], str) and value["timestamp"].endswith("Z"))
    )
    assert value["source"] == "collector-r5"
    assert by_point["gen_power"]["key"] == field_power


async def test_unmapped_points_are_filtered(r5_sink_factory, r5_stack, tmp_path: Path) -> None:
    topic = f"r5-topic-{uuid.uuid4().hex[:8]}"
    field_speed = f"wh.r5.{DEVICE_ID}.rotor_speed"
    sinks = _kafka_sink(
        topic=topic,
        bootstrap=r5_stack.kafka,
        points=[_mapping("rotor_speed", field_speed)],
    )
    config_dir = write_r5_config(tmp_path / "cfg", free_port(), r5_stack.nats, sinks)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("kafka_sink",))
    await wait_state(sink, "kafka_sink", "running")

    # 未映射点：assembly 的 allowed 集合直接丢弃，不得产生 Kafka 消息。
    await publish(r5_stack, make_sample("gen_power", 999.0, sample_id="r5k-filtered"), "realtime")
    await publish(r5_stack, make_sample("rotor_speed", 7.25, sample_id="r5k-kept"), "realtime")

    async def _kept() -> list[dict[str, Any]] | None:
        messages = await _drain_topic(r5_stack.kafka, topic, timeout=6.0)
        if any(m["value"]["point_id"] == "rotor_speed" for m in messages):
            return messages
        return None

    messages = await wait_until(_kept, timeout=40.0, interval=2.0, description="mapped point seen")
    assert all(m["value"]["point_id"] == "rotor_speed" for m in messages)
    assert not any(m["value"]["point_id"] == "gen_power" for m in messages)


async def test_broker_outage_reports_failure_and_resumes(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    topic = f"r5-topic-{uuid.uuid4().hex[:8]}"
    field_speed = f"wh.r5.{DEVICE_ID}.rotor_speed"
    sinks = _kafka_sink(
        topic=topic,
        bootstrap=r5_stack.kafka,
        points=[_mapping("rotor_speed", field_speed)],
    )
    config_dir = write_r5_config(tmp_path / "cfg", free_port(), r5_stack.nats, sinks)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("kafka_sink",))
    await wait_state(sink, "kafka_sink", "running")

    r5_stack.stop_container("kafka")
    try:
        # send_and_wait 必须传播 broker 不可达：禁止"无错误的成功确认"。
        await publish(
            r5_stack, make_sample("rotor_speed", 501.0, sample_id="r5k-outage"), "realtime"
        )

        async def _failed() -> dict[str, Any] | None:
            status = await mgmt(sink, "GetSinkStatus", "kafka_sink")
            if status["state"] == "failed" and status["failure"]:
                return status
            return None

        failed = await wait_until(_failed, timeout=20.0, interval=0.5, description="kafka failed")
        assert "Kafka" in str(failed["failure"])
    finally:
        r5_stack.start_container("kafka")

    # supervisor 每 5s 重启；producer 重新建连，新消息继续写入同一 topic。
    await wait_state(sink, "kafka_sink", "running", timeout=90.0)
    await publish(r5_stack, make_sample("rotor_speed", 502.0, sample_id="r5k-resume"), "realtime")

    async def _resumed() -> dict[str, Any] | None:
        messages = await _drain_topic(r5_stack.kafka, topic, timeout=8.0)
        for message in messages:
            if message["value"]["point_id"] == "rotor_speed" and message["value"]["value"] == 502.0:
                return message
        return None

    resumed = await wait_until(
        _resumed, timeout=60.0, interval=3.0, description="post-outage write"
    )
    assert resumed["key"] == field_speed
    status = await mgmt(sink, "GetSinkStatus", "kafka_sink")
    assert status["failure"] is None


async def test_management_stop_start_restart(r5_sink_factory, r5_stack, tmp_path: Path) -> None:
    topic = f"r5-topic-{uuid.uuid4().hex[:8]}"
    field_speed = f"wh.r5.{DEVICE_ID}.rotor_speed"
    sinks = _kafka_sink(
        topic=topic,
        bootstrap=r5_stack.kafka,
        points=[_mapping("rotor_speed", field_speed)],
    )
    config_dir = write_r5_config(tmp_path / "cfg", free_port(), r5_stack.nats, sinks)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("kafka_sink",))
    await wait_state(sink, "kafka_sink", "running")

    await mgmt(sink, "StopSink", "kafka_sink")
    await wait_state(sink, "kafka_sink", "stopped")
    await publish(r5_stack, make_sample("rotor_speed", 601.0, sample_id="r5k-stopped"), "realtime")
    # 停止期间 Core 消息无人订阅；确认窗口内 topic 无该值。
    import asyncio

    await asyncio.sleep(2.0)
    quiet = await _drain_topic(r5_stack.kafka, topic, timeout=4.0)
    assert not any(m["value"]["value"] == 601.0 for m in quiet)

    await mgmt(sink, "StartSink", "kafka_sink")
    await wait_state(sink, "kafka_sink", "running")
    await publish(r5_stack, make_sample("rotor_speed", 602.0, sample_id="r5k-started"), "realtime")

    async def _seen(value: float) -> dict[str, Any] | None:
        messages = await _drain_topic(r5_stack.kafka, topic, timeout=6.0)
        for message in messages:
            if message["value"]["value"] == value:
                return message
        return None

    await wait_until(lambda: _seen(602.0), timeout=45.0, interval=3.0, description="after start")

    await mgmt(sink, "RestartSink", "kafka_sink")
    await wait_state(sink, "kafka_sink", "running")
    await publish(
        r5_stack, make_sample("rotor_speed", 603.0, sample_id="r5k-restarted"), "realtime"
    )
    await wait_until(lambda: _seen(603.0), timeout=45.0, interval=3.0, description="after restart")
