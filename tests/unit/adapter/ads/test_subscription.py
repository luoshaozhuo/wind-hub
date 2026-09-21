"""Unit tests for ADS device-notification subscription.

Covers the connection pool (split at ``max_notifications_per_connection``), the
set-difference ``subscribe`` behaviour, and the thread-safe callback → event-loop
delivery path.  ``pyads.Connection`` / ``pyads.NotificationAttrib`` are replaced
by fakes; the callback is invoked directly to simulate pyads' worker thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from wind_hub.adapter.outbound.protocol.ads.config import ADSConfig
from wind_hub.adapter.outbound.protocol.ads.mapping import ADSPoint
from wind_hub.adapter.outbound.protocol.ads.subscription import ADSSubscription
from wind_hub.domain.model.point import PointValue


def _config(max_notifications_per_connection: int = 550) -> ADSConfig:
    return ADSConfig(
        ams_net_id="",
        target_net_id="",
        max_notifications_per_connection=max_notifications_per_connection,
    )


def _point(point_id: str, symbol: str) -> ADSPoint:
    return ADSPoint(
        point_id=point_id,
        index_group=0,
        index_offset=0,
        data_type="REAL",
        size=4,
        symbol=symbol,
    )


class FakeNotificationAttrib:
    def __init__(self, length: int, cycle_time: float = 0.02, max_delay: float = 0.06) -> None:
        self.length = length
        self.cycle_time = cycle_time
        self.max_delay = max_delay


class FakeConnection:
    """Synchronous stand-in for ``pyads.Connection`` tracking notification handles."""

    instances: list[FakeConnection] = []

    def __init__(self, net_id: object, port: object, host: object) -> None:
        self.is_open = False
        self.callbacks: dict[str, object] = {}
        self.del_count = 0
        FakeConnection.instances.append(self)

    def set_timeout(self, ms: int) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def add_device_notification(
        self, symbol: str, attr: object, callback: object, user_handle: object = None
    ) -> tuple[object, object]:
        handle: tuple[object, object] = (symbol, "handle")
        self.callbacks[symbol] = callback
        return (handle, user_handle)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        self.del_count += 1


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeConnection.instances = []
    monkeypatch.setattr("pyads.Connection", FakeConnection)
    monkeypatch.setattr("pyads.NotificationAttrib", FakeNotificationAttrib)


async def _received(
    points: list[ADSPoint], symbol: str, value: object, config: ADSConfig | None = None
) -> tuple[ADSSubscription, list[PointValue]]:
    received: list[PointValue] = []

    async def on_data(pv: PointValue) -> None:
        received.append(pv)

    sub = ADSSubscription(
        config=config or _config(),
        device_id="test-dev",
        host="192.168.0.100",
        loop=asyncio.get_running_loop(),
        on_data=on_data,
        cycle_time=0.02,
    )
    await sub.subscribe(points)
    conn = sub._connections[0]  # noqa: SLF001
    conn.callbacks[symbol](None, symbol, None, value)
    await asyncio.sleep(0.05)  # let the loop run the drain coroutine
    return sub, received


class TestRegistration:
    async def test_subscribe_registers_notifications(self, patched: None) -> None:
        sub = ADSSubscription(
            config=_config(),
            device_id="test-dev",
            host="192.168.0.100",
            loop=asyncio.get_running_loop(),
            on_data=_noop,
            cycle_time=0.02,
        )
        await sub.subscribe([_point("a", "MAIN.a"), _point("b", "MAIN.b")])

        assert len(sub._connections) == 1  # noqa: SLF001
        assert set(sub._handles) == {"MAIN.a", "MAIN.b"}  # noqa: SLF001
        await sub.close()

    async def test_connection_split_at_threshold(self, patched: None) -> None:
        sub = ADSSubscription(
            config=_config(max_notifications_per_connection=2),
            device_id="test-dev",
            host="192.168.0.100",
            loop=asyncio.get_running_loop(),
            on_data=_noop,
            cycle_time=0.02,
        )
        await sub.subscribe([_point("a", "MAIN.a"), _point("b", "MAIN.b"), _point("c", "MAIN.c")])

        assert len(sub._connections) == 2  # noqa: SLF001
        assert sub._loads == [2, 1]  # noqa: SLF001
        assert set(sub._handles) == {"MAIN.a", "MAIN.b", "MAIN.c"}  # noqa: SLF001
        await sub.close()

    async def test_subscribe_computes_set_difference(self, patched: None) -> None:
        sub = ADSSubscription(
            config=_config(),
            device_id="test-dev",
            host="192.168.0.100",
            loop=asyncio.get_running_loop(),
            on_data=_noop,
            cycle_time=0.02,
        )
        await sub.subscribe([_point("a", "MAIN.a"), _point("b", "MAIN.b")])
        await sub.subscribe([_point("b", "MAIN.b"), _point("c", "MAIN.c")])

        assert set(sub._handles) == {"MAIN.b", "MAIN.c"}  # noqa: SLF001
        # only MAIN.a was unregistered
        assert sum(c.del_count for c in FakeConnection.instances) == 1
        await sub.close()


class TestDelivery:
    async def test_notification_delivers_point_value(self, patched: None) -> None:
        sub, received = await _received(
            [_point("rotor.speed", "MAIN.rotorSpeed")], "MAIN.rotorSpeed", 1200.5
        )

        assert len(received) == 1
        assert received[0].device_id == "test-dev"
        assert received[0].point_id == "rotor.speed"
        assert received[0].value == 1200.5
        assert received[0].quality.value == "good"
        assert received[0].source == "ads"
        assert isinstance(received[0].timestamp, datetime)
        await sub.close()

    async def test_close_unregisters_all(self, patched: None) -> None:
        sub = ADSSubscription(
            config=_config(),
            device_id="test-dev",
            host="192.168.0.100",
            loop=asyncio.get_running_loop(),
            on_data=_noop,
            cycle_time=0.02,
        )
        await sub.subscribe([_point("a", "MAIN.a"), _point("b", "MAIN.b")])
        await sub.close()

        assert sub._handles == {}  # noqa: SLF001
        assert sum(c.del_count for c in FakeConnection.instances) == 2


async def _noop(pv: PointValue) -> None:
    return None
