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

from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.ads.config import ADSConfig
from wind_hub_core.protocol.ads.mapping import ADSPoint
import wind_hub_core.protocol.ads.subscription as subscription_module
from wind_hub_core.protocol.ads.subscription import ADSSubscription


def _config(max_notifications_per_connection: int = 550) -> ADSConfig:
    return ADSConfig(
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
        self.callbacks: dict[tuple[int, int], object] = {}
        self.del_count = 0
        self.read_state_error: Exception | None = None
        FakeConnection.instances.append(self)

    def set_timeout(self, ms: int) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read_state(self) -> tuple[int, int]:
        if self.read_state_error is not None:
            raise self.read_state_error
        return (5, 0)

    def notification(self, plc_datatype: object) -> object:
        def decorator(callback: object) -> object:
            return callback

        return decorator

    def add_device_notification(
        self,
        address: tuple[int, int],
        attr: object,
        callback: object,
        user_handle: object = None,
    ) -> tuple[object, object]:
        handle: tuple[object, object] = (address, "handle")
        self.callbacks[address] = callback
        return (handle, user_handle)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        self.del_count += 1


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeConnection.instances = []
    monkeypatch.setattr("pyads.Connection", FakeConnection)
    monkeypatch.setattr("pyads.NotificationAttrib", FakeNotificationAttrib)


async def _received(
    points: list[ADSPoint],
    address: tuple[int, int],
    value: object,
    config: ADSConfig | None = None,
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
    conn.callbacks[address](None, address, None, value)
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
        assert set(sub._handles) == {"a", "b"}  # noqa: SLF001
        assert sub.healthy is True
        await sub.close()
        assert sub.healthy is False

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
        assert set(sub._handles) == {"a", "b", "c"}  # noqa: SLF001
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

        assert set(sub._handles) == {"b", "c"}  # noqa: SLF001
        # only point a was unregistered
        assert sum(c.del_count for c in FakeConnection.instances) == 1
        await sub.close()


class TestRecovery:
    async def test_connection_loss_rebuilds_pool_and_reregisters(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subscription_module, "_HEALTH_CHECK_INTERVAL", 0.01)
        sub = ADSSubscription(
            config=_config(),
            device_id="test-dev",
            host="192.168.0.100",
            loop=asyncio.get_running_loop(),
            on_data=_noop,
            cycle_time=0.02,
        )
        await sub.subscribe([_point("a", "MAIN.a"), _point("b", "MAIN.b")])
        first = FakeConnection.instances[0]
        first.read_state_error = OSError("PLC offline")

        for _ in range(20):
            await asyncio.sleep(0.01)
            if len(FakeConnection.instances) >= 2 and set(sub._handles) == {"a", "b"}:
                break

        assert len(FakeConnection.instances) >= 2
        assert first.is_open is False
        assert set(sub._handles) == {"a", "b"}
        assert sub._connections[0] is not first
        assert sub.healthy is True
        await sub.close()



class TestDelivery:
    async def test_notification_delivers_point_value(self, patched: None) -> None:
        sub, received = await _received(
            [_point("rotor.speed", "MAIN.rotorSpeed")], (0, 0), 1200.5
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
