"""KafkaSink × 真实 Kafka broker 集成测试。

被测组件是 :class:`KafkaSink`（含真实 AIOKafkaProducer，不做任何
替换）；验证侧是**独立** AIOKafkaConsumer——消息必须真正经过 broker。
broker 由 ``kafka_service`` fixture 提供（环境变量优先，否则
Docker Compose 自动拉起）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.support.wait import wait_kafka_messages
from wind_hub_collector.adapter.outbound.sink.mq.kafka import KafkaSink
from wind_hub_core.config.schema import SinkConfig
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.point import PointValue

# 默认路径由 Docker Compose 拉起真实服务（外部实例经环境变量接管）；
# 服务真实性显式标注，不计入 mock。
pytestmark = [pytest.mark.docker, pytest.mark.real_service]


def _pv(point_id: str, value: object, device_id: str = "modbus-1") -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        timestamp=datetime(2026, 10, 2, 8, 0, 0, tzinfo=UTC),
        source="integration-test",
    )


def _topic() -> str:
    return f"windhub-test-{uuid.uuid4().hex[:12]}"


def _sink(bootstrap: str, topic: str, **params: object) -> KafkaSink:
    return KafkaSink(
        SinkConfig(
            name="kafka",
            type="kafka",
            params={"bootstrap_servers": bootstrap, "topic": topic, **params},
        )
    )


class TestKafkaProduce:
    async def test_write_reaches_broker(self, kafka_service: str) -> None:
        topic = _topic()
        sink = _sink(kafka_service, topic)
        await sink.open()
        try:
            await sink.write([_pv("rotor.speed", 1200.5), _pv("temp.int", 25)])
            await sink.flush()

            messages = await wait_kafka_messages(
                kafka_service, topic, min_messages=2, timeout=30.0
            )
            by_point = {p["point_id"]: p for p in messages}
            assert by_point["rotor.speed"]["value"] == 1200.5
            assert by_point["rotor.speed"]["device_id"] == "modbus-1"
            assert by_point["rotor.speed"]["quality"] == "good"
            assert by_point["rotor.speed"]["source"] == "integration-test"
            assert by_point["temp.int"]["value"] == 25
            assert sink.health().healthy is True
        finally:
            await sink.close()

    async def test_key_field_sets_message_key(self, kafka_service: str) -> None:
        topic = _topic()
        sink = _sink(kafka_service, topic, key_field="device_id")
        await sink.open()
        try:
            await sink.write([_pv("rotor.speed", 1.0, device_id="turbine-7")])
            await sink.flush()
            messages = await wait_kafka_messages(
                kafka_service, topic, min_messages=1, timeout=30.0
            )
            assert messages[0]["_key"] == "turbine-7"
        finally:
            await sink.close()

    async def test_close_is_idempotent(self, kafka_service: str) -> None:
        sink = _sink(kafka_service, _topic())
        await sink.open()
        await sink.close()
        await sink.close()


class TestKafkaConfigValidation:
    def test_missing_bootstrap_rejected(self) -> None:
        with pytest.raises(ConfigError, match="bootstrap_servers"):
            KafkaSink(SinkConfig(name="kafka", type="kafka", params={"topic": "t"}))

    def test_missing_topic_rejected(self, kafka_service: str) -> None:
        with pytest.raises(ConfigError, match="topic"):
            KafkaSink(
                SinkConfig(
                    name="kafka",
                    type="kafka",
                    params={"bootstrap_servers": kafka_service},
                )
            )

    def test_invalid_key_field_rejected(self, kafka_service: str) -> None:
        with pytest.raises(ConfigError, match="key_field"):
            _sink(kafka_service, "t", key_field="value")
