"""SampleBatch v2 批量编解码与发布管线吞吐/延迟基线（无外部服务）。

按 testing.md §6：无正式 SLA 时只建立 baseline，报告实测环境、轮次、吞吐量
与延迟分位数，不虚构生产阈值。Fake NATS 仅隔离 socket I/O；计时覆盖真实
的 SampleItem/SampleBatch 构造、严格校验、JSON 编码与解码全路径。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from collector.infrastructure import publisher as publisher_module
from collector.infrastructure.adapters.outbound.publisher import CollectorPublisher
from contracts.sample_batch import SampleBatch
from core.application import ProtocolSample

pytestmark = [pytest.mark.performance, pytest.mark.load]


class _FakeJetStream:
    async def publish(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeNats:
    def __init__(self) -> None:
        self.calls = 0
        self.is_connected = True

    async def publish(self, subject: str, payload: bytes) -> None:
        self.calls += 1

    def jetstream(self) -> _FakeJetStream:
        return _FakeJetStream()

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _samples(points: int) -> tuple[ProtocolSample, ...]:
    now = datetime.now(UTC)
    return tuple(
        ProtocolSample(point_id=f"turbine.point.{i:04d}", value=i * 0.5, timestamp=now)
        for i in range(points)
    )


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def pct(p: float) -> float:
        index = min(len(ordered) - 1, int(len(ordered) * p))
        return ordered[index]

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


@pytest.mark.parametrize(
    ("mode", "points_per_batch", "batches"),
    [("realtime", 100, 300), ("reliable", 500, 60)],
)
def test_batch_encode_decode_throughput_baseline(
    mode: str, points_per_batch: int, batches: int
) -> None:
    samples = _samples(points_per_batch)
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm-a",
        collector_id="collector-1",
        delivery=mode,
        capacity=4096,
        max_samples_per_batch=points_per_batch,
    )
    encoded: list[bytes] = []
    encode_latency_ms: list[float] = []
    decode_latency_ms: list[float] = []
    started = time.perf_counter()
    for i in range(batches):
        batch = publisher._pack(f"wtg-{i % 30:02d}", tuple(publisher._item(s) for s in samples))
        assert len(batch) == 1
        t0 = time.perf_counter()
        payload = batch[0].encode()
        encode_latency_ms.append((time.perf_counter() - t0) * 1000)
        encoded.append(payload)
        t1 = time.perf_counter()
        decoded = SampleBatch.decode(payload, mode)  # type: ignore[arg-type]
        decode_latency_ms.append((time.perf_counter() - t1) * 1000)
        assert len(decoded.samples) == points_per_batch
    elapsed = time.perf_counter() - started
    total_samples = batches * points_per_batch
    payload_bytes = statistics.mean(len(p) for p in encoded)
    baseline = {
        "environment": f"pytest in-process, python {pytest.__version__}",
        "mode": mode,
        "batches": batches,
        "points_per_batch": points_per_batch,
        "total_samples": total_samples,
        "elapsed_s": round(elapsed, 4),
        "throughput_samples_per_s": round(total_samples / elapsed, 1),
        "mean_payload_bytes": round(payload_bytes, 1),
        "encode_ms": {k: round(v, 4) for k, v in _percentiles(encode_latency_ms).items()},
        "decode_ms": {k: round(v, 4) for k, v in _percentiles(decode_latency_ms).items()},
    }
    print(f"\n[throughput-baseline] {baseline}")
    # 只断言可重复的正确性与有界性，不虚构 SLA 阈值。
    assert total_samples > 0 and all(len(p) > 0 for p in encoded)


@pytest.mark.asyncio
async def test_publisher_pipeline_throughput_with_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNats()

    async def fake_connect(**kwargs: Any) -> _FakeNats:
        return fake

    monkeypatch.setattr(publisher_module.nats, "connect", fake_connect)
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm-a",
        collector_id="collector-1",
        delivery="realtime",
        capacity=4096,
        max_samples_per_batch=100,
        # 本用例验证管线排空与计数，不验证 staleness 淘汰。默认 500ms 预算在
        # 全仓并发负载下会让排队较久的批次被生产逻辑合法判过期丢弃，形成环境
        # 相关的假失败；放大预算使过期路径与本测量解耦，dropped==0 断言不变。
        max_staleness_ms=30_000,
    )
    await publisher.start()
    samples = _samples(100)
    batches = 300  # 30 台 × 10 批
    started = time.perf_counter()
    for i in range(batches):
        await publisher.publish(f"wtg-{i % 30:02d}", samples)
    # 确定性完成条件：先等到全部批次真正送达传输层，再 stop()。stop() 内的
    # queue.join() 只有 2s 生产保护超时，负载竞争下直接 stop 会把未排空批次
    # 计入 dropped，造成墙钟抖动型假失败；轮询实际送达数不改变任何生产指标。
    try:
        drain_deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < drain_deadline:
            if fake.calls == batches:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(f"publisher drain stalled: {fake.calls}/{batches} delivered")
    finally:
        await publisher.stop()
    elapsed = time.perf_counter() - started
    assert fake.calls == batches
    assert publisher.published_samples == batches * 100
    assert publisher.dropped == 0 and publisher.failed == 0
    print(
        f"\n[publisher-baseline] batches={batches} elapsed_s={elapsed:.4f} "
        f"throughput_samples_per_s={batches * 100 / elapsed:.1f}"
    )
