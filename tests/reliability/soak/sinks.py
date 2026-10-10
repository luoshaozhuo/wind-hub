"""soak 专用的计量 sink——在真实 sink（含 NullSink）外做一层透明计量包装。

包装发生在装配期（``sink_factories`` 覆盖内装饰），被包装的是完整真实 sink
（KafkaSink / DBSink 的全部网络行为不变）；这不是 monkeypatch——端口接口
是生产定义的装饰点，计量维度（收到点数、重复点、写耗时）是 soak 验收的
必备观测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from collector.application.sink_port import SinkPort
from collector.domain.point_value import PointValue
from core.application import ConnectionHealth
from tests.reliability.soak.metrics import append_bounded

#: 延迟样本上限（超上限隔点抽稀，见 :func:`append_bounded`）——24h 高节拍
#: soak 下批次样本可达千万级，必须有界。
LATENCY_SAMPLE_CAP = 100_000


@dataclass
class SinkStats:
    """计量 sink 的累计观测。"""

    received: int = 0
    """``write()`` 成功落盘/落服务的总点数。"""
    duplicates: int = 0
    """(device_id, point_id, timestamp) 完全重复的点数——数据重复验收。"""
    latencies_ms: list[float] = field(default_factory=list)
    """每批「最早采集时间戳 → write 返回」的耗时样本（端到端 sink 延迟，
    有界抽稀——见 :data:`LATENCY_SAMPLE_CAP`）。"""


class RecordingSink(SinkPort):
    """包装任意 SinkPort 的计量 sink；``inner`` 为 None 时仅计数（等效 NullSink）。

    重复口径：同一点的新点值时间戳与上一次完全相同即重复——采集重放/队列
    重发都会被识别；正常轮询相邻周期时间戳不同，不误报。内存按「每点一条
    最后时间戳」有界（O(点数)），24h soak 也不会膨胀。
    """

    def __init__(self, inner: SinkPort | None = None) -> None:
        self._inner = inner
        self.stats = SinkStats()
        self._last_ts: dict[tuple[str, str], str] = {}

    async def open(self) -> None:
        if self._inner is not None:
            await self._inner.open()

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()

    async def write(self, batch: list[PointValue]) -> None:
        if not batch:
            return
        earliest = min(pv.timestamp for pv in batch)
        if self._inner is not None:
            await self._inner.write(batch)
        # write 成功才计数——失败批次由生产重试/丢弃语义处理，不算收到。
        self.stats.received += len(batch)
        append_bounded(
            self.stats.latencies_ms,
            (datetime.now(UTC) - earliest).total_seconds() * 1000.0,
            LATENCY_SAMPLE_CAP,
        )
        for pv in batch:
            key = (pv.device_id, pv.point_id)
            iso = pv.timestamp.isoformat()
            if self._last_ts.get(key) == iso:
                self.stats.duplicates += 1
            else:
                self._last_ts[key] = iso

    async def flush(self) -> None:
        if self._inner is not None:
            await self._inner.flush()

    def health(self) -> ConnectionHealth:
        if self._inner is not None:
            return self._inner.health()
        return ConnectionHealth(healthy=True)


def percentile(values: list[float], pct: float) -> float:
    """最近秩百分位（与 perf collector 同口径）；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    import math

    rank = math.ceil(len(ordered) * pct / 100.0)
    return ordered[max(0, rank - 1)]


def monotonic_s() -> float:
    """测试内统一的单调时钟读取 seam。"""
    return time.monotonic()
