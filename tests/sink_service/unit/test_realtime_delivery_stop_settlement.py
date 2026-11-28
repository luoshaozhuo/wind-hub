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
