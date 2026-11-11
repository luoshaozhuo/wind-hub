"""R13（v1_47）replay 消息头 ACK 绕过回归（无 broker 确定性单测）。

首轮验收记录的缺口：旧 ``_run`` 读取 ``Wh-Replay-Delivery`` 头，头存在且
不等于本 Delivery ID 时直接判 terminal 并 ``ack_sync``——外部可伪造头让
Durable 对未实际交付的消息提前 ACK。生产修复删除该分支后，terminal 只能
来自 ``_deliver_batch`` 的真实交付结果。

本测试用 fake subscription/message 驱动 ``ReliableDeliveryPort._run`` 单轮，
钉死三条契约（真实 JetStream 端到端证据由 system 层同名用例补充）：

1. 伪造他人 Delivery 的 replay 头：消息仍进入真实交付，成功后才 ACK；
2. 伪造头 + 交付未终局：必须 NAK 且绝不 ACK（头不能制造 terminal=True）；
3. 无头消息行为不变（对照组）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from nats.errors import TimeoutError as NatsTimeoutError

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryPort
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files

pytestmark = [pytest.mark.unit, pytest.mark.reliability]

_DEFINITION = DeliveryDefinition("power_history", "history_kafka", "reliable", "START")
_FORGED_HEADER = {"Wh-Replay-Delivery": "some-other-delivery-id"}


class _FakeMessage:
    def __init__(self, batch: SampleBatch, headers: dict[str, str] | None) -> None:
        self.data = batch.encode()
        self.subject = batch.subject()
        self.headers = headers
        self.metadata = SimpleNamespace(
            num_pending=0,
            num_delivered=1,
            timestamp=datetime.now(UTC),
        )
        self.ack_calls = 0
        self.nak_calls = 0
        self.in_progress_calls = 0

    async def ack_sync(self, timeout: float = 0.0) -> None:
        self.ack_calls += 1

    async def nak(self, delay: float | None = None) -> None:
        self.nak_calls += 1

    async def in_progress(self) -> None:
        self.in_progress_calls += 1


class _FakeSubscription:
    def __init__(self, message: _FakeMessage, port: ReliableDeliveryPort) -> None:
        self._message = message
        self._port = port
        self._served = False

    async def fetch(self, batch: int = 1, timeout: float = 1.0) -> list[_FakeMessage]:
        if not self._served:
            self._served = True
            return [self._message]
        self._port._stop.set()
        raise NatsTimeoutError


def _reliable_batch() -> SampleBatch:
    now = datetime.now(UTC)
    return SampleBatch(
        batch_id="b-replay-bypass-qa",
        mode="reliable",
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-qa",
        device_id="WT001",
        publish_time=now,
        samples=(
            SampleItem(
                point_id="p001",
                sample_id="s-replay-bypass-1",
                value=1.0,
                quality="good",
                sample_time=now,
                max_staleness_ms=None,
            ),
        ),
    )


def _port() -> ReliableDeliveryPort:
    snapshot = validate_snapshot(snapshot_files(snapshot_docs()))
    return ReliableDeliveryPort(_DEFINITION, snapshot, pool=object())


async def _run_one_cycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    headers: dict[str, str] | None,
    terminal: bool,
) -> tuple[_FakeMessage, ReliableDeliveryPort]:
    port = _port()
    message = _FakeMessage(_reliable_batch(), headers)
    port._subscription = _FakeSubscription(message, port)

    deliver_calls = 0

    async def _fake_deliver(msg: Any, batch: SampleBatch) -> bool:
        nonlocal deliver_calls
        deliver_calls += 1
        return terminal

    async def _no_capacity_check() -> None:
        return None

    monkeypatch.setattr(port, "_deliver_batch", _fake_deliver)
    monkeypatch.setattr(port, "_check_capacity", _no_capacity_check)

    await port._run()
    assert deliver_calls == 1, "消息必须进入真实交付路径，头不能跳过派发"
    return message, port


async def test_forged_replay_header_still_delivers_and_acks_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message, port = await _run_one_cycle(monkeypatch, headers=_FORGED_HEADER, terminal=True)
    assert message.ack_calls == 1
    assert message.nak_calls == 0
    assert port.acknowledged_batches_total == 1


async def test_forged_replay_header_cannot_force_ack_when_delivery_unsettled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 旧实现下头存在且属主不匹配会直接 terminal=True 提前 ACK；
    # 修复后未终局只能 NAK，JetStream 保留重投责任。
    message, port = await _run_one_cycle(monkeypatch, headers=_FORGED_HEADER, terminal=False)
    assert message.ack_calls == 0
    assert message.nak_calls == 1
    assert port.acknowledged_batches_total == 0
    assert port.retry_total == 1


async def test_headerless_message_keeps_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    message, port = await _run_one_cycle(monkeypatch, headers=None, terminal=True)
    assert message.ack_calls == 1
    assert message.nak_calls == 0
    assert port.acknowledged_batches_total == 1
