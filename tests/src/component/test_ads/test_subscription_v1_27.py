"""ADS 原生订阅 v1_27 新增语义的组件测试（FakeAdsSession，非真实 PLC）。

覆盖设计 v1_27 要求：
- notification_trigger cyclic/on_change 校验与 pyads 原生 trans_mode 映射；
- 首次注册与点表热更新使用相同触发模式；
- 队列满丢弃计数（2 的幂告警路径由计数器与日志旁证）；
- 业务回调异常计数且消费循环不退出；
- 无时区/非法设备时间回退接收端 UTC，不伪造设备时钟；
- 热更新代次隔离：旧代次原生回调不再入队，旧句柄被注销。

Fake 只隔离原生 I/O，不冒充 ADS/TwinCAT 协议；真实 PLC 链路属 hardware 资格。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from ctypes import c_ubyte
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pyads
import pytest

from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.ads.subscription import ADSSubscription
from core.infrastructure.protocol.mapping_barrier import MappingBarrier

pytestmark = [
    pytest.mark.component,
    pytest.mark.ads,
    pytest.mark.mock_service,
    pytest.mark.lifecycle,
]

_AWARE = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class FakeAdsSession:
    """记录 add/del_device_notification 的参数，原样返回内部回调以便驱动。"""

    def __init__(self) -> None:
        self.registrations: list[dict[str, Any]] = []
        self.deletions: list[dict[str, Any]] = []
        self._token = 0

    def notification(
        self, datatype: object
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        assert datatype == c_ubyte * 2

        def decorate(callback: Callable[..., None]) -> Callable[..., None]:
            return callback

        return decorate

    async def execute(self, operation: str, **kwargs: Any) -> tuple[int, int] | None:
        if operation == "add_device_notification":
            self._token += 1
            token = (self._token, 0)
            self.registrations.append({**kwargs, "token": token})
            return token
        if operation == "del_device_notification":
            self.deletions.append(kwargs)
            return None
        raise AssertionError(f"unexpected operation: {operation}")


def _point() -> ADSPoint:
    return ADSPoint(
        point_id="p",
        index_group=0x4020,
        index_offset=100,
        data_type="INT",
        size=2,
        symbol="MAIN.p",
    )


def _subscription(
    session: FakeAdsSession,
    *,
    trigger: str = "cyclic",
    queue_capacity: int = 1000,
    callback: Callable[[tuple[ProtocolSample, ...]], Any] | None = None,
) -> tuple[ADSSubscription, list[tuple[ProtocolSample, ...]], asyncio.Event]:
    delivered: list[tuple[ProtocolSample, ...]] = []
    progressed = asyncio.Event()

    async def default_callback(samples: tuple[ProtocolSample, ...]) -> None:
        if callback is not None:
            await callback(samples)
        delivered.append(samples)
        progressed.set()

    sub = ADSSubscription(
        session,  # type: ignore[arg-type]
        (_point(),),
        default_callback,
        interval=0.1,
        trigger=trigger,  # type: ignore[arg-type]
        max_block_bytes=20,
        max_delay=0.01,
        queue_capacity=queue_capacity,
        barrier=MappingBarrier(),
    )
    return sub, delivered, progressed


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=5)
    event.clear()


def test_constructor_rejects_invalid_trigger_interval_and_capacity() -> None:
    session = FakeAdsSession()

    async def cb(_samples: object) -> None:
        return None

    common = {
        "points": (_point(),),
        "callback": cb,
        "interval": 0.1,
        "max_block_bytes": 20,
        "max_delay": 0.01,
        "queue_capacity": 10,
        "barrier": MappingBarrier(),
    }
    with pytest.raises(ValueError, match="trigger"):
        ADSSubscription(session, trigger="on_edge", **common)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="interval"):
        ADSSubscription(session, trigger="cyclic", **{**common, "interval": 0})
    with pytest.raises(ValueError, match="capacity"):
        ADSSubscription(session, trigger="cyclic", **{**common, "queue_capacity": 0})


@pytest.mark.asyncio
async def test_trigger_maps_to_native_trans_mode_and_stays_stable_on_hot_update() -> None:
    for trigger, native in (
        ("cyclic", pyads.ADSTRANS_SERVERCYCLE),
        ("on_change", pyads.ADSTRANS_SERVERONCHA),
    ):
        session = FakeAdsSession()
        sub, _, _ = _subscription(session, trigger=trigger)
        await sub.start()
        try:
            assert session.registrations[0]["attr"].trans_mode == native
            candidate = await sub.prepare_update((_point(),))
            sub.commit_update(candidate)  # 同步提交
            assert session.registrations[1]["attr"].trans_mode == native
        finally:
            await sub.stop()


@pytest.mark.asyncio
async def test_naive_plc_timestamp_falls_back_to_receiver_utc() -> None:
    session = FakeAdsSession()
    sub, delivered, progressed = _subscription(session)
    await sub.start()
    try:
        naive = datetime(2026, 11, 8, 12, 0, 0)  # 无时区：不得被当作权威 UTC
        sub._callbacks[0](1, ("ig", 100), naive, (c_ubyte * 2)(1, 0))
        await _wait(progressed)
        sample = delivered[0][0]
        assert sample.timestamp_source == "local"
        assert sample.timestamp.tzinfo is not None
        assert sample.receive_time is not None
    finally:
        await sub.stop()


@pytest.mark.asyncio
async def test_aware_device_timestamp_is_converted_to_utc() -> None:
    session = FakeAdsSession()
    sub, delivered, progressed = _subscription(session)
    await sub.start()
    try:
        cst = timezone(timedelta(hours=8))
        device_time = datetime(2026, 11, 8, 20, 0, tzinfo=cst)
        sub._callbacks[0](1, ("ig", 100), device_time, (c_ubyte * 2)(1, 0))
        await _wait(progressed)
        sample = delivered[0][0]
        assert sample.timestamp_source == "device"
        assert sample.timestamp == datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    finally:
        await sub.stop()


@pytest.mark.asyncio
async def test_invalid_device_clock_does_not_kill_callback_and_falls_back_local() -> None:
    session = FakeAdsSession()
    sub, delivered, progressed = _subscription(session)
    await sub.start()
    try:

        class BrokenTime(datetime):
            def utcoffset(self) -> timedelta | None:  # type: ignore[override]
                raise OverflowError("device clock out of range")

        broken = BrokenTime(2026, 11, 8, 12, 0, tzinfo=UTC)
        sub._callbacks[0](1, ("ig", 100), broken, (c_ubyte * 2)(1, 0))
        await _wait(progressed)
        sample = delivered[0][0]
        assert sample.timestamp_source == "local"
        assert sample.timestamp.tzinfo is not None
    finally:
        await sub.stop()


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest_and_counts_without_worker_running() -> None:
    # 不 start：无消费协程，_enqueue 的容量淘汰行为可确定性验证。
    session = FakeAdsSession()
    sub, _, _ = _subscription(session, queue_capacity=1)
    samples = (ProtocolSample(point_id="p", value=1, timestamp=_AWARE),)
    sub._enqueue(0, samples)
    assert sub.dropped_notifications == 0
    sub._enqueue(0, samples)
    sub._enqueue(0, samples)
    assert sub.dropped_notifications == 2
    queued = sub._queue.get_nowait()
    assert queued == (0, samples)


@pytest.mark.asyncio
async def test_business_callback_failure_is_counted_and_worker_survives() -> None:
    session = FakeAdsSession()
    should_fail = {"on": True}

    async def callback(samples: tuple[ProtocolSample, ...]) -> None:
        if should_fail["on"]:
            raise RuntimeError("downstream consumer exploded")

    sub, delivered, progressed = _subscription(session, callback=callback)
    await sub.start()
    try:
        sub._callbacks[0](1, ("ig", 100), _AWARE, (c_ubyte * 2)(1, 0))
        deadline = asyncio.get_event_loop().time() + 5
        while sub.callback_failures == 0 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert sub.callback_failures == 1
        should_fail["on"] = False
        sub._callbacks[0](1, ("ig", 100), _AWARE, (c_ubyte * 2)(2, 0))
        await _wait(progressed)
        assert sub.callback_failures == 1  # 成功后不再累加
        assert delivered[-1][0].value == 2
    finally:
        await sub.stop()


@pytest.mark.asyncio
async def test_stale_generation_callback_is_ignored_after_hot_update() -> None:
    session = FakeAdsSession()
    sub, delivered, progressed = _subscription(session, queue_capacity=10)
    await sub.start()
    try:
        old_generation_callback = sub._callbacks[0]
        candidate = await sub.prepare_update((_point(),))
        sub.commit_update(candidate)
        new_generation_callback = sub._callbacks[1]
        old_generation_callback(1, ("ig", 100), _AWARE, (c_ubyte * 2)(9, 0))
        await asyncio.sleep(0.05)
        assert delivered == []
        new_generation_callback(2, ("ig", 100), _AWARE, (c_ubyte * 2)(7, 0))
        await _wait(progressed)
        assert delivered[-1][0].value == 7
        # 提交后旧句柄进入 retired，cleanup 触发 del_device_notification。
        await sub.cleanup_retired()
        assert session.deletions
    finally:
        await sub.stop()
