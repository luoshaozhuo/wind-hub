"""ADS 订阅投递语义测试（Component，fake pyads 连接）。

与 test_subscription_lifecycle.py 互补，聚焦数据面：

- 多个区块独立到达，字段不跨区块串扰；
- 原生时标缺失/naive/aware 三种情况下的时标回退；
- 区块原始长度不符时整批 Quality.BAD；
- 慢消费者下队列满丢弃最旧整个区块（端到端，经真实 Worker）；
- 关闭（deactivate）后迟到的原生回调不再入队，队列被清空。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application.protocol_contract import ProtocolSample, Quality
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.ads.subscription import ADSSubscription, _normalize_timestamp


def _point(name: str, offset: int, kind: str = "REAL", size: int = 4) -> ADSPoint:
    return ADSPoint(name, 0x4020, offset, kind, size)


class _Connection:
    """记录多区块注册并允许按 handle 触发任意原始字节。"""

    def __init__(self) -> None:
        self.is_open = True
        self.callbacks: dict[int, Callable[..., None]] = {}
        self.deleted: list[object] = []
        self.registrations: list[tuple[int, int, int]] = []
        self._next = 1

    def notification(
        self, datatype: object
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        del datatype

        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            return callback

        return decorator

    def add_device_notification(
        self, address: tuple[int, int], attr: object, callback: Callable[..., None]
    ) -> tuple[object, object]:
        del attr
        handle = self._next
        self._next += 1
        self.callbacks[handle] = callback
        return (handle, None)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        del user_handle
        if isinstance(handle, int):
            self.callbacks.pop(handle, None)
            self.deleted.append(handle)

    def run(self, name: str, *args: object) -> object:
        return getattr(self, name)(*args)

    async def execute(self, name: str, *args: object) -> object:
        # 测试替身：对齐生产 _AdsSession.execute 的异步命令入口契约。
        return self.run(name, *args)

    def fire(self, handle: int, raw: bytes, *, timestamp: Any = None) -> None:
        self.callbacks[handle](handle, (0x4020, 0), timestamp, raw)


class _Attrib:
    def __init__(self, length: int, *, trans_mode: int, cycle_time: float) -> None:
        self.length = length
        self.trans_mode = trans_mode
        self.cycle_time = cycle_time


class _FakePyads:
    ADSTRANS_SERVERCYCLE = 3
    NotificationAttrib = _Attrib


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> _Connection:
    connection = _Connection()
    monkeypatch.setattr(subscription_module, "pyads", _FakePyads)
    return connection


@pytest.mark.asyncio
async def test_multiple_blocks_deliver_only_their_own_fields(
    fake_pyads: _Connection,
) -> None:
    received: list[tuple[str, object, Quality]] = []
    received_batches: list[tuple[str, ...]] = []
    got_second = asyncio.Event()

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        received_batches.append(tuple(sample.point_id for sample in samples))
        for sample in samples:
            received.append((sample.point_id, sample.value, sample.quality))
            if len(received) == 3:
                got_second.set()

    subscription = ADSSubscription(
        fake_pyads,
        (_point("a", 0), _point("b", 4), _point("c", 2000)),
        on_sample,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
        max_block_bytes=64,
    )
    await subscription.start()
    try:
        # 两个区块：[a,b]@0 size 8 与 [c]@2000 size 4。
        assert len(fake_pyads.callbacks) == 2
        first_handle, second_handle = 1, 2
        fake_pyads.fire(first_handle, struct.pack("<ff", 1.5, -2.0))
        fake_pyads.fire(second_handle, struct.pack("<f", 7.5))
        await asyncio.wait_for(got_second.wait(), timeout=1.0)
        assert received_batches == [("a", "b"), ("c",)]
        assert received == [
            ("a", 1.5, Quality.GOOD),
            ("b", -2.0, Quality.GOOD),
            ("c", 7.5, Quality.GOOD),
        ]
    finally:
        await subscription.close()
    assert fake_pyads.deleted == [1, 2]


@pytest.mark.asyncio
async def test_raw_length_mismatch_delivers_bad_quality_batch(
    fake_pyads: _Connection,
) -> None:
    received: list[ProtocolSample] = []
    done = asyncio.Event()

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            received.append(sample)
            if len(received) == 2:
                done.set()

    subscription = ADSSubscription(
        fake_pyads,
        (_point("a", 0), _point("b", 4)),
        on_sample,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    await subscription.start()
    try:
        # 区块声明 8 字节，原生回调只给 2 字节：整批 BAD，异常不跨线程逃逸。
        fake_pyads.fire(1, b"\x01\x02")
        await asyncio.wait_for(done.wait(), timeout=1.0)
        assert [(s.point_id, s.value, s.quality) for s in received] == [
            ("a", None, Quality.BAD),
            ("b", None, Quality.BAD),
        ]
    finally:
        await subscription.close()


@pytest.mark.asyncio
async def test_local_fallback_timestamp_marked_local(
    fake_pyads: _Connection,
) -> None:
    received: list[ProtocolSample] = []
    done = asyncio.Event()

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            received.append(sample)
            done.set()

    subscription = ADSSubscription(
        fake_pyads,
        (_point("a", 0),),
        on_sample,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    await subscription.start()
    try:
        # timestamp=None（pyads 未提供时标）→ 本地 UTC 回退。
        fake_pyads.fire(1, struct.pack("<f", 1.0), timestamp=None)
        await asyncio.wait_for(done.wait(), timeout=1.0)
        assert received[0].timestamp_source == "local"
        assert received[0].timestamp.tzinfo is not None
    finally:
        await subscription.close()


def test_timestamp_normalization_paths() -> None:
    # 非 datetime（pyads 未给时标）→ 本地 UTC 接收时间。
    stamp, source = _normalize_timestamp(None)
    assert source == "local"
    assert stamp.tzinfo is not None
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 5

    # naive datetime 视为设备本地时标，标注 UTC 不做位移。
    naive = datetime(2026, 1, 2, 3, 4, 5)
    stamp, source = _normalize_timestamp(naive)
    assert source == "device"
    assert stamp == naive.replace(tzinfo=UTC)

    # aware datetime 转换到 UTC。
    cst = timezone(timedelta(hours=8))
    stamp, source = _normalize_timestamp(naive.replace(tzinfo=cst))
    assert source == "device"
    assert stamp == datetime(2026, 1, 1, 19, 4, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_slow_consumer_drops_oldest_whole_blocks_end_to_end(
    fake_pyads: _Connection,
) -> None:
    gate = asyncio.Event()
    received: list[float] = []

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            await gate.wait()
            received.append(float(sample.value))  # type: ignore[arg-type]  # 测试点为 REAL

    subscription = ADSSubscription(
        fake_pyads,
        (_point("a", 0),),
        on_sample,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    subscription._queue = asyncio.Queue(maxsize=2)
    await subscription.start()
    try:
        # 前两个批次占满队列（Worker 被 gate 阻塞），随后的批次挤掉最旧批次。
        for value in range(6):
            fake_pyads.fire(1, struct.pack("<f", float(value)))
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert subscription._dropped_count >= 1
        gate.set()
        # 等待队列排空。
        for _ in range(100):
            await asyncio.sleep(0.01)
            if subscription._queue.empty():
                break
        # 批次 0 已被 Worker 取出（在途，不在队列）必然投递；队列中最旧
        # 的批次 1..3 随整块丢弃，仅保留最后两个批次。
        assert received == [0.0, 4.0, 5.0]
    finally:
        gate.set()
        await subscription.close()
    assert subscription._queue.empty()


@pytest.mark.asyncio
async def test_late_native_callback_after_deactivate_is_dropped(
    fake_pyads: _Connection,
) -> None:
    delivered = asyncio.Event()

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            del sample
            delivered.set()

    subscription = ADSSubscription(
        fake_pyads,
        (_point("a", 0),),
        on_sample,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    await subscription.start()
    late_callback = fake_pyads.callbacks[1]

    # 只做 deactivate（模拟关闭流程中“先停投递再注销”的窗口）。
    subscription.deactivate()
    late_callback(1, (0x4020, 0), None, struct.pack("<f", 9.0))
    for _ in range(10):
        await asyncio.sleep(0)
    assert subscription._queue.empty()
    assert not delivered.is_set()

    await subscription.stop_worker()
    assert subscription._worker_task is None
    assert fake_pyads.deleted == []  # stop_worker 不发注销命令
