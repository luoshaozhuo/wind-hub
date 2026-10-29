"""soak 的 Sink 侧计量：在 NATS 边界录制 Collector 发出的 SampleMessage。

Sink Service 独立服务化后，Collector 进程内不再存在 Sink 包装与背压队列。
soak 的「sink 收到/重复/延迟」口径随之落到消息总线边界：本录制器以真实
NATS 订阅（Core NATS realtime / JetStream reliable）消费 Collector 发布的
样本，按 ``sample_id`` 计重复、按 ``sample_time → 收到时刻`` 计端到端延迟。

注意：这只覆盖 Collector → NATS → 订阅者一段；下游 Redis/TDengine 目标
写入延迟属于 sink_service 自身的 soak 范畴。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import nats

from contracts.sample_message import DeliveryMode, SampleMessage


def percentile(values: list[float], pct: float) -> float:
    """返回百分位（pct∈[0,100]）；空序列返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct / 100.0
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


@dataclass
class RecorderStats:
    """录制器统计（全部在 NATS 回调线程外只读消费）。"""

    received: int = 0
    duplicates: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def record(self, message: SampleMessage, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        self.received += 1
        if message.sample_id in self._seen:
            self.duplicates += 1
        else:
            self._seen.add(message.sample_id)
        self.latencies_ms.append((moment - message.sample_time).total_seconds() * 1000.0)


class NatsSampleRecorder:
    """真实 NATS 订阅录制器：open 订阅、close drain 释放。"""

    def __init__(
        self,
        servers: str | tuple[str, ...],
        *,
        subject: str = "wh.rt.>",
        delivery: DeliveryMode = "realtime",
        durable: str | None = None,
        stream: str = "WH_HISTORY",
    ) -> None:
        self._servers = [servers] if isinstance(servers, str) else list(servers)
        self._subject = subject
        self._delivery = delivery
        self._durable = durable
        self._stream = stream
        self._client = None
        self._subscription = None
        self.stats = RecorderStats()

    async def open(self) -> None:
        client = await nats.connect(servers=self._servers)
        try:
            if self._delivery == "realtime":
                subscription = await client.subscribe(self._subject, cb=self._on_message)
            else:
                subscription = await client.jetstream().subscribe(
                    self._subject,
                    durable=self._durable,
                    stream=self._stream,
                    manual_ack=True,
                    cb=self._on_reliable,
                )
        except BaseException:
            await client.close()
            raise
        self._client = client
        self._subscription = subscription

    async def _on_message(self, msg) -> None:  # type: ignore[no-untyped-def]
        message = SampleMessage.decode(msg.data, "realtime")
        self.stats.record(message)

    async def _on_reliable(self, msg) -> None:  # type: ignore[no-untyped-def]
        try:
            message = SampleMessage.decode(msg.data, "reliable")
            self.stats.record(message)
        finally:
            await msg.ack()

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.drain()
        finally:
            await client.close()
