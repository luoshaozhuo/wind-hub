"""Unit tests for the real KafkaSink (aiokafka producer mocked).

覆盖：构造期参数校验、``open`` 创建生产者并传入正确参数、幂等 open/close、
UTF-8 JSON 序列化、``key_field`` 消息 key、投递失败抛 ``SinkError``、以及
连续失败达到阈值后报告 unhealthy。

``AIOKafkaProducer`` 通过 monkeypatch 替换为内存假实现，不发起任何真实
网络连接。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from wind_hub_collector.adapter.outbound.sink.mq.kafka import KafkaSink
from wind_hub_core.config.schema import SinkConfig
from wind_hub_core.model.errors import ConfigError, SinkError
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue, Quality

_TS = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

_MODULE = "wind_hub_collector.adapter.outbound.sink.mq.kafka"


class _FakeProducer:
    """内存假生产者：记录构造参数与投递消息，可注入 start/send/broker-ack 失败。

    ``send`` 与 aiokafka 一致：入队即返回一个 ``asyncio.Future``，broker
    ack 结果（成功 / ``fail_broker`` 时的异常）通过 future 异步暴露。
    """

    instances: list[_FakeProducer] = []
    fail_start = False
    fail_send = False
    fail_broker = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sent: list[tuple[str, bytes, bytes | None]] = []
        self.started = False
        self.stopped = False
        _FakeProducer.instances.append(self)

    async def start(self) -> None:
        if _FakeProducer.fail_start:
            raise OSError("broker unreachable")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def flush(self) -> None:
        return None

    async def send(self, topic: str, value: bytes, key: bytes | None) -> asyncio.Future[None]:
        if _FakeProducer.fail_send:
            raise OSError("send failed")
        self.sent.append((topic, value, key))
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if _FakeProducer.fail_broker:
            future.set_exception(OSError("broker nack"))
        else:
            future.set_result(None)
        return future


@pytest.fixture
def fake_producer(monkeypatch: pytest.MonkeyPatch) -> type[_FakeProducer]:
    _FakeProducer.instances = []
    _FakeProducer.fail_start = False
    _FakeProducer.fail_send = False
    _FakeProducer.fail_broker = False
    monkeypatch.setattr(f"{_MODULE}.AIOKafkaProducer", _FakeProducer)
    return _FakeProducer


def _cfg(**params: Any) -> SinkConfig:
    return SinkConfig(
        name="s1",
        type="kafka",
        params={"bootstrap_servers": "localhost:9092", "topic": "t", **params},
    )


def _pv(point_id: str = "p1", value: Any = 1.0, device_id: str = "d1") -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        quality=Quality.GOOD,
        timestamp=_TS,
        source="modbus",
    )


def _sent_json(sent: tuple[str, bytes, bytes | None]) -> dict[str, Any]:
    return json.loads(sent[1].decode("utf-8"))


# ---------------------------------------------------------------------------
# 构造期参数校验
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_missing_bootstrap_servers_raises(self) -> None:
        with pytest.raises(ConfigError, match="bootstrap_servers"):
            KafkaSink(SinkConfig(name="s1", type="kafka", params={"topic": "t"}))

    def test_missing_topic_raises(self) -> None:
        with pytest.raises(ConfigError, match="topic"):
            KafkaSink(SinkConfig(name="s1", type="kafka", params={"bootstrap_servers": "x:9092"}))

    def test_invalid_key_field_raises(self) -> None:
        with pytest.raises(ConfigError, match="key_field"):
            KafkaSink(_cfg(key_field="not_a_field"))

    def test_invalid_batch_size_raises(self) -> None:
        with pytest.raises(ConfigError, match="batch_size"):
            KafkaSink(_cfg(batch_size=0))

    def test_invalid_linger_ms_raises(self) -> None:
        with pytest.raises(ConfigError, match="linger_ms"):
            KafkaSink(_cfg(linger_ms=-5))

    def test_defaults_applied(self) -> None:
        sink = KafkaSink(_cfg())
        assert sink._acks == "all"
        assert sink._batch_size == 16384
        assert sink._linger_ms == 0
        assert sink._key_field is None

    def test_valid_construction_is_healthy(self, fake_producer: type[_FakeProducer]) -> None:
        assert KafkaSink(_cfg()).health() == HealthStatus(healthy=True)


# ---------------------------------------------------------------------------
# open / close 生命周期
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_creates_producer_with_params(
        self, fake_producer: type[_FakeProducer]
    ) -> None:
        sink = KafkaSink(_cfg(compression_type="gzip", acks=1))
        await sink.open()

        assert len(_FakeProducer.instances) == 1
        producer = _FakeProducer.instances[0]
        assert producer.started is True
        assert producer.kwargs["bootstrap_servers"] == "localhost:9092"
        assert producer.kwargs["acks"] == 1
        assert producer.kwargs["compression_type"] == "gzip"
        await sink.close()

    async def test_open_idempotent(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        await sink.open()
        assert len(_FakeProducer.instances) == 1
        await sink.close()

    async def test_open_failure_raises_sink_error(self, fake_producer: type[_FakeProducer]) -> None:
        _FakeProducer.fail_start = True
        sink = KafkaSink(_cfg())
        with pytest.raises(SinkError, match="open failed"):
            await sink.open()

    async def test_close_stops_producer(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        await sink.close()
        assert _FakeProducer.instances[0].stopped is True
        await sink.close()  # 重复 close 幂等

    async def test_write_before_open_raises(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg())
        with pytest.raises(SinkError, match="before open"):
            await sink.write([_pv()])


# ---------------------------------------------------------------------------
# 序列化与投递
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_serializes_utf8_json_with_all_fields(
        self, fake_producer: type[_FakeProducer]
    ) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        await sink.write([_pv(value=800.0)])
        await sink.close()

        (topic, value, key), *_ = _FakeProducer.instances[0].sent
        assert topic == "t"
        assert key is None
        assert _sent_json((topic, value, key)) == {
            "device_id": "d1",
            "point_id": "p1",
            "value": 800.0,
            "quality": "good",
            "timestamp": "2026-09-16T12:00:00+00:00",
            "source": "modbus",
        }

    async def test_no_key_field_sends_none_key(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        await sink.write([_pv(device_id="d1")])
        assert _FakeProducer.instances[0].sent[0][2] is None
        await sink.close()

    async def test_key_field_sets_partition_key(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg(key_field="device_id"))
        await sink.open()
        await sink.write([_pv(device_id="turbine-01")])
        assert _FakeProducer.instances[0].sent[0][2] == b"turbine-01"
        await sink.close()

    async def test_empty_batch_is_noop(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        await sink.write([])
        assert _FakeProducer.instances[0].sent == []
        await sink.close()


# ---------------------------------------------------------------------------
# 健康状态
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_single_failure_keeps_healthy(self, fake_producer: type[_FakeProducer]) -> None:
        _FakeProducer.fail_send = True
        sink = KafkaSink(_cfg())
        await sink.open()
        with pytest.raises(SinkError):
            await sink.write([_pv()])
        assert sink.health().healthy is True
        await sink.close()

    async def test_repeated_failures_mark_unhealthy(
        self, fake_producer: type[_FakeProducer]
    ) -> None:
        _FakeProducer.fail_send = True
        sink = KafkaSink(_cfg())
        await sink.open()
        for _ in range(5):
            with pytest.raises(SinkError):
                await sink.write([_pv()])
        assert sink.health().healthy is False
        assert "write failed" in (sink.health().message or "")
        await sink.close()

    async def test_success_after_failures_recovers(
        self, fake_producer: type[_FakeProducer]
    ) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        _FakeProducer.fail_send = True
        for _ in range(4):
            with pytest.raises(SinkError):
                await sink.write([_pv()])
        assert sink.health().healthy is True  # 未达阈值
        _FakeProducer.fail_send = False
        await sink.write([_pv()])  # 成功复位计数
        assert sink.health().healthy is True
        await sink.close()


# ---------------------------------------------------------------------------
# broker-ack 失败核对（决策 3）
# ---------------------------------------------------------------------------


class TestBrokerAckFailures:
    async def test_write_reaps_completed_broker_failures(
        self, fake_producer: type[_FakeProducer]
    ) -> None:
        """假生产者的 ack 异常在 send 时即刻落定 → 同一次 write 末尾收割到。"""
        sink = KafkaSink(_cfg())
        await sink.open()
        _FakeProducer.fail_broker = True
        await sink.write([_pv()])  # send 本身成功（入队），ack 失败由收割发现
        assert sink.health().healthy is True  # 仅 1 次失败，未达阈值
        assert "broker ack failed" in (sink.health().message or "")
        await sink.close()

    async def test_flush_counts_pending_broker_failures(
        self, fake_producer: type[_FakeProducer]
    ) -> None:
        """write 时未决的 future 在 flush 时 gather 核对，ack 失败累计计数。"""
        sink = KafkaSink(_cfg())
        await sink.open()
        _FakeProducer.fail_broker = True
        await sink.write([_pv(), _pv(), _pv()])
        assert sink._consecutive_failures == 3  # noqa: SLF001  假实现同步落定，已收割

        # 重新制造一批未决 future：直接注入，验证 flush 的 gather 路径
        loop = asyncio.get_running_loop()
        pending: list[asyncio.Future[None]] = []
        for _ in range(2):
            fut: asyncio.Future[None] = loop.create_future()
            fut.set_exception(OSError("broker nack"))
            pending.append(fut)
        sink._pending_futures = pending  # noqa: SLF001
        await sink.flush()
        assert sink._consecutive_failures == 5  # noqa: SLF001
        assert sink.health().healthy is False  # 达到阈值
        await sink.close()

    async def test_flush_success_resets_failures(self, fake_producer: type[_FakeProducer]) -> None:
        sink = KafkaSink(_cfg())
        await sink.open()
        _FakeProducer.fail_broker = True
        await sink.write([_pv()])
        assert sink._consecutive_failures == 1  # noqa: SLF001
        _FakeProducer.fail_broker = False
        await sink.flush()  # 无未决失败 → 复位
        assert sink._consecutive_failures == 0  # noqa: SLF001
        assert sink.health() == HealthStatus(healthy=True)
        await sink.close()
