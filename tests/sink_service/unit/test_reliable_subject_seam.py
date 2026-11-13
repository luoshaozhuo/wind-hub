"""Reliable Delivery 设备级 Subject 与 Durable 冲突 fail-closed（单元层，fake NATS）。

覆盖设计 §6.3.3 与 reliable_runtime.start()：

- 并发 1.1.0 模板：pull_subscribe / ConsumerConfig.filter_subject 使用与
  ``SampleBatch.subject()`` 完全一致的四段精确 Subject；
- 旧 1.0.0 模板：保留 ``...>`` 宽订阅，行为不退化；
- 已存在 Durable 的 filter_subject 与期望不一致时抛 ConfigurationError，
  并闭合 NATS 客户端与 Sink 池引用，不得静默沿用旧 Durable。
"""

from __future__ import annotations

from typing import Any

import pytest

from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure import reliable_runtime
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryPort
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
    _delivery_instance,
    service_document,
    snapshot_files,
    template_doc,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

KAFKA_110 = f"{TEMPLATE_ROOT}/sinks/kafka_history_v1.1.0.yaml"
KAFKA_100 = f"{TEMPLATE_ROOT}/sinks/kafka_history_v1.0.0.yaml"
DELIVERY_TEMPLATE = f"{TEMPLATE_ROOT}/deliveries/reliable_history_v1.0.0.yaml"


def _kafka_template(*, version: str, concurrent: bool, inflight: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "file_version": version,
        "template_id": "kafka_history",
        "kind": "kafka",
        "capabilities": {
            "realtime": False,
            "reliable": True,
            "idempotent": False,
            "supports_concurrent_write": concurrent,
            "max_safe_inflight": inflight,
        },
        "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
        "output": {
            "topic": "windhub.samples",
            "key_field": "device_id",
            "business_duplicate_policy": "allow_at_least_once",
            "dedup_identity_fields": ["delivery_id", "sample_id"],
            "enable_idempotence": True,
            "acks": "all",
            "write_timeout_ms": 2000,
            "linger_ms": 0,
        },
    }


def _snapshot(*, concurrent: bool, devices: list[str]) -> Any:
    version = "1.1.0" if concurrent else "1.0.0"
    docs = {
        SERVICE_FILE: service_document(),
        KAFKA_110 if concurrent else KAFKA_100: _kafka_template(
            version=version, concurrent=concurrent, inflight=4 if concurrent else 1
        ),
        DELIVERY_TEMPLATE: template_doc("reliable_history"),
        SINKS_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "sinks-r7",
            "sinks": {
                "history_kafka": {
                    "template_id": "kafka_history",
                    "template_version": version,
                    "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
                    "overrides": {},
                }
            },
        },
        DELIVERIES_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "deliveries-r7",
            "deliveries": {
                "d1": _delivery_instance(
                    "d1", template="reliable_history", sink="history_kafka", devices=devices
                )
            },
        },
    }
    return validate_snapshot(snapshot_files(docs))


class _FakeAdapter:
    def __init__(self) -> None:
        self.io_inflight = False
        self.settled = False

    async def settle(self) -> None:
        self.settled = True


class _FakePool:
    def __init__(self) -> None:
        self.adapter = _FakeAdapter()
        self.releases = 0

    async def acquire(self, sink_id: str, snapshot: Any) -> _FakeAdapter:
        return self.adapter

    async def release(self, sink_id: str) -> None:
        self.releases += 1


class _StreamConfig:
    def __init__(self, subjects: list[str]) -> None:
        self.subjects = subjects
        self.storage = "file"
        self.discard = "new"
        self.retention = "limits"
        self.max_bytes = 2 * 1024**3
        self.max_age = 24 * 3600


class _FakeStream:
    def __init__(self, subjects: list[str]) -> None:
        self.config = _StreamConfig(subjects)


class _FakeConsumerConfig:
    def __init__(self, filter_subject: str, ack_wait: float = 30.0) -> None:
        from nats.js.api import AckPolicy

        self.max_deliver = -1
        self.max_ack_pending = 1
        self.ack_policy = AckPolicy.EXPLICIT
        self.filter_subject = filter_subject
        # v1_47 绑定前守卫新增 AckWait 核对，默认与 service 夹具的 30s 一致。
        self.ack_wait = ack_wait


class _FakeConsumerInfo:
    def __init__(self, filter_subject: str, ack_wait: float = 30.0) -> None:
        self.config = _FakeConsumerConfig(filter_subject, ack_wait)
        self.num_pending = 0
        self.num_ack_pending = 0


class _FakeSubscription:
    async def fetch(self, batch: int, timeout: float) -> list[Any]:
        from nats.errors import TimeoutError as NatsTimeoutError

        raise NatsTimeoutError


class _FakeJS:
    def __init__(self, existing_filter: str | None) -> None:
        self.existing_filter = existing_filter
        self.requested_subject: str | None = None
        self.consumer_config: Any = None
        self._bound = False

    async def stream_info(self, name: str) -> _FakeStream:
        if name == "WH_HISTORY":
            return _FakeStream(["wh.hist.batch.>"])
        return _FakeStream(["wh.dlq"])

    async def pull_subscribe(
        self, subject: str, *, durable: str, stream: str, config: Any
    ) -> _FakeSubscription:
        self.requested_subject = subject
        self.consumer_config = config
        self._bound = True
        return _FakeSubscription()

    async def consumer_info(self, stream: str, durable: str) -> _FakeConsumerInfo:
        # 真实 broker：无 Durable 时 consumer_info 抛 NotFound；绑定后回显
        # pull_subscribe 请求的配置（含 AckWait），供二次一致性核对。
        if self.existing_filter is not None:
            return _FakeConsumerInfo(self.existing_filter)
        if not self._bound:
            from nats.js.errors import NotFoundError

            raise NotFoundError
        return _FakeConsumerInfo(self.requested_subject, self.consumer_config.ack_wait)


class _FakeClient:
    def __init__(self, js: _FakeJS) -> None:
        self._js = js
        self.is_connected = True
        self.closed = False

    def jetstream(self) -> _FakeJS:
        return self._js

    async def close(self) -> None:
        self.closed = True


def _patch_nats(monkeypatch: pytest.MonkeyPatch, js: _FakeJS) -> _FakeClient:
    client = _FakeClient(js)

    async def fake_connect(*args: Any, **kwargs: Any) -> _FakeClient:
        return client

    monkeypatch.setattr(reliable_runtime.nats, "connect", fake_connect)
    return client


def _port(snapshot: Any, pool: _FakePool) -> ReliableDeliveryPort:
    definition = DeliveryDefinition(
        delivery_id="d1", sink_id="history_kafka", channel="reliable", desired_state="START"
    )
    return ReliableDeliveryPort(definition, snapshot, pool)  # type: ignore[arg-type]


async def test_concurrent_template_uses_exact_device_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(concurrent=True, devices=["WT001"])
    js = _FakeJS(existing_filter=None)
    client = _patch_nats(monkeypatch, js)
    pool = _FakePool()
    port = _port(snapshot, pool)
    await port.start()
    try:
        expected = "wh.hist.batch.farm-a.collector-1.WT001"
        assert js.requested_subject == expected
        assert js.consumer_config.filter_subject == expected
        assert js.consumer_config.max_ack_pending == 1
    finally:
        await port.stop()
    assert client.closed and pool.releases == 1


async def test_legacy_template_keeps_broad_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(concurrent=False, devices=["WT001", "WT002"])
    js = _FakeJS(existing_filter=None)
    _patch_nats(monkeypatch, js)
    pool = _FakePool()
    port = _port(snapshot, pool)
    await port.start()
    try:
        assert js.requested_subject == "wh.hist.batch.farm-a.collector-1.>"
        assert js.consumer_config.filter_subject.endswith(".>")
    finally:
        await port.stop()


async def test_durable_filter_mismatch_fails_closed_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(concurrent=True, devices=["WT001"])
    # 旧 Durable 仍绑定宽订阅：新精确 Subject 必须在任何变更调用前拒绝复用。
    js = _FakeJS(existing_filter="wh.hist.batch.farm-a.collector-1.>")
    client = _patch_nats(monkeypatch, js)
    pool = _FakePool()
    port = _port(snapshot, pool)
    with pytest.raises(ConfigurationError, match="must be migrated before rebinding"):
        await port.start()
    assert client.closed
    assert pool.releases == 1
    # 守卫发生在 pull_subscribe 之前：既有 Consumer 未被修改，端口未绑定。
    assert js.requested_subject is None
    assert not js._bound  # noqa: SLF001
    assert port._subscription is None  # noqa: SLF001
    assert port._adapter is None  # noqa: SLF001


class _MismatchedAckWaitJS(_FakeJS):
    """模拟已存在 Durable 仅 AckWait 与当前配置不一致。"""

    async def consumer_info(self, stream: str, durable: str) -> _FakeConsumerInfo:
        if self.existing_filter is not None and not self._bound:
            return _FakeConsumerInfo(self.existing_filter, ack_wait=999.0)
        return await super().consumer_info(stream, durable)


async def test_durable_ackwait_mismatch_rejects_before_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(concurrent=True, devices=["WT001"])
    expected_subject = "wh.hist.batch.farm-a.collector-1.WT001"
    js = _MismatchedAckWaitJS(existing_filter=expected_subject)
    client = _patch_nats(monkeypatch, js)
    pool = _FakePool()
    port = _port(snapshot, pool)
    with pytest.raises(ConfigurationError, match="must be migrated before rebinding"):
        await port.start()
    assert client.closed
    assert not js._bound  # noqa: SLF001
    assert port._subscription is None  # noqa: SLF001
