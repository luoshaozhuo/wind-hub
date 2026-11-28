"""A timed-out Delivery stop must retain ownership until native I/O settles."""

from __future__ import annotations

import asyncio

import pytest

from sink_service.application.latency_window import LatencyWindow
from sink_service.application.realtime_delivery import RealtimeDeliveryPort
from sink_service.domain.config import ConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.reliability]


class _Engine:
    def __init__(self) -> None:
        self.stops = 0
        self.received_total = 0
        self.dropped_total = 0
        self.delivered_total = 0
        self.failed_total = 0
        self.write_success_total = 0
        self.latency_window = LatencyWindow(window_ms=60000)

    async def stop(self) -> None:
        self.stops += 1


class _Ingress:
    def __init__(self) -> None:
        self.removes = 0

    async def remove(self, _delivery_id: str) -> None:
        self.removes += 1


class _Pool:
    def __init__(self) -> None:
        self.releases = 0

    async def release(self, _sink_id: str) -> None:
        self.releases += 1


class _Writer:
    def __init__(self) -> None:
        self.settled = asyncio.Event()

    async def settle(self) -> None:
        await self.settled.wait()


async def test_timed_out_stop_keeps_native_ownership_and_can_be_retried() -> None:
    port = object.__new__(RealtimeDeliveryPort)
    port._id = "d1"
    port._sink_id = "sink1"
    port._engine = _Engine()
    port._ingress = _Ingress()
    port._pool = _Pool()
    port._writer = _Writer()
    port._stopping = False
    port._settle_timeout = 0.01
    port._retained = {}
    port._retained_latency = LatencyWindow(window_ms=60000)

    with pytest.raises(ConfigurationError, match="settlement timed out"):
        await port.stop()
    assert port._stopping
    assert port._engine is not None
    assert port._writer is not None
    assert port._pool.releases == 0

    port._writer.settled.set()
    await port.stop()
    assert not port._stopping
    assert port._engine is None
    assert port._writer is None
    assert port._pool.releases == 1
    assert port._ingress.removes == 2


async def test_repeated_stop_keeps_timing_out_until_native_io_settles() -> None:
    """未结算 I/O 期间连续 stop 都必须超时，且不释放任何资源。"""
    port = object.__new__(RealtimeDeliveryPort)
    port._id = "d1"
    port._sink_id = "sink1"
    port._engine = _Engine()
    port._ingress = _Ingress()
    port._pool = _Pool()
    port._writer = _Writer()
    port._stopping = False
    port._settle_timeout = 0.01
    port._retained = {}
    port._retained_latency = LatencyWindow(window_ms=60000)

    for _ in range(3):
        with pytest.raises(ConfigurationError, match="settlement timed out"):
            await port.stop()
        assert port._stopping
        assert port._pool.releases == 0
        assert port._engine is not None

    port._writer.settled.set()
    await port.stop()
    assert not port._stopping
    assert port._pool.releases == 1


async def test_start_retries_cleanup_only_after_prior_stop_settles() -> None:
    """start() 在 _stopping 时先重试 stop；清理完成后才允许继续。"""
    port = object.__new__(RealtimeDeliveryPort)
    port._id = "d1"
    port._sink_id = "sink1"
    port._engine = _Engine()
    port._ingress = _Ingress()
    port._pool = _Pool()
    port._writer = _Writer()
    port._stopping = True
    port._settle_timeout = 0.01
    port._retained = {}
    port._retained_latency = LatencyWindow(window_ms=60000)

    stop_calls = 0

    async def fake_stop() -> None:
        nonlocal stop_calls
        stop_calls += 1
        port._stopping = False

    port.stop = fake_stop  # type: ignore[method-assign]
    await port.start()
    # start 必须先经过 stop 重试；清理后引擎仍在则直接返回，不再重复获取资源。
    assert stop_calls == 1
    assert not port._stopping


async def test_stop_clears_obsolete_stopping_state_only_without_resources() -> None:
    """引擎与写入器均不存在时，stop 才允许清掉过时的 _stopping 标记。"""
    port = object.__new__(RealtimeDeliveryPort)
    port._id = "d1"
    port._sink_id = "sink1"
    port._engine = None
    port._ingress = _Ingress()
    port._pool = _Pool()
    port._writer = None
    port._stopping = True
    port._settle_timeout = 0.01
    port._retained = {}
    port._retained_latency = LatencyWindow(window_ms=60000)

    await port.stop()
    assert not port._stopping
    assert port._pool.releases == 0


async def test_stop_with_writer_but_no_engine_keeps_stopping_flag() -> None:
    """异常态（引擎空但写入器仍在）不得清 _stopping，也不得释放配额。"""
    port = object.__new__(RealtimeDeliveryPort)
    port._id = "d1"
    port._sink_id = "sink1"
    port._engine = None
    port._ingress = _Ingress()
    port._pool = _Pool()
    port._writer = _Writer()
    port._stopping = True
    port._settle_timeout = 0.01
    port._retained = {}
    port._retained_latency = LatencyWindow(window_ms=60000)

    await port.stop()
    assert port._stopping
    assert port._pool.releases == 0
