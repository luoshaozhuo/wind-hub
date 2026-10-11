from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import ClassVar
from typing import Protocol as TypingProtocol

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application import (
    ConfigError,
    ProtocolSample,
    ProtocolWrite,
)
from core.application.settings import ADSLocalConfig
from core.domain import (
    UNIT_CATALOG,
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.ads import ADSDriver


class _Closable(TypingProtocol):
    async def close(self) -> None: ...


def _endpoint(
    *,
    port: int | None = 801,
) -> ConnectionEndpoint:
    return ConnectionEndpoint("192.0.2.20", port)


def _point(
    point_id: str,
    *,
    ext: dict[str, str | int | float | bool | None] | None = None,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext={} if ext is None else ext,
    )


def _point_ext(
    *,
    data_type: str = "REAL",
    **options: str | int | float | bool | None,
) -> dict[str, str | int | float | bool | None]:
    return {"data_type": data_type, **options}


class _FakeADSConnection:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, object, object]] = []

    def write(
        self,
        index_group: int,
        index_offset: int,
        value: object,
        datatype: object,
    ) -> None:
        self.writes.append((index_group, index_offset, value, datatype))


class _FakePyads:
    PLCTYPE_DINT = object()

    def __init__(self) -> None:
        self.open_calls = 0
        self.close_calls = 0
        self.local_addresses: list[str] = []

    def open_port(self) -> None:
        self.open_calls += 1

    def close_port(self) -> None:
        self.close_calls += 1

    def set_local_address(self, value: str) -> None:
        self.local_addresses.append(value)


@pytest.mark.asyncio
async def test_ads_write_coerces_integral_float_for_integer_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _point(
        "setpoint",
        ext=_point_ext(
            data_type="DINT",
            index_group=0x4020,
            index_offset=12,
        ),
    )
    table = PointTable(
        PointTableId("ads_pt"),
        Protocol("ads"),
        {"setpoint": point},
    )
    driver = ADSDriver(
        _endpoint(),
        table,
        {},
    )
    fake_connection = _FakeADSConnection()
    driver._connection = fake_connection
    driver._connected = True

    fake_pyads = _FakePyads()
    monkeypatch.setattr(driver_module, "pyads", fake_pyads)

    result = await driver.write_one(ProtocolWrite(point_id=point.point_id, value=10.0))

    assert result.success is True
    assert fake_connection.writes[0][2] == 10
    assert type(fake_connection.writes[0][2]) is int


@pytest.mark.asyncio
async def test_ads_driver_local_ams_identity_initializes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyads = _FakePyads()
    monkeypatch.setattr(driver_module, "pyads", fake_pyads)
    monkeypatch.setattr(sys, "platform", "linux")
    config = ADSLocalConfig("192.0.2.10.1.2", "192.0.2.10", "user", "secret")
    try:
        await ADSDriver.initialize_local(config)
        await ADSDriver.initialize_local(config)
        assert fake_pyads.open_calls == 1
        assert fake_pyads.local_addresses == ["192.0.2.10.1.2"]
        with pytest.raises(ConfigError, match="conflicts"):
            await ADSDriver.initialize_local(
                ADSLocalConfig("192.0.2.11.1.2", "192.0.2.11", "user", "secret")
            )
    finally:
        await ADSDriver.close_local()
    assert fake_pyads.close_calls == 1


class _FakeNotificationAttrib:
    last_mode: ClassVar[int]

    def __init__(
        self,
        length: int,
        *,
        cycle_time: float,
        max_delay: float,
        trans_mode: int,
    ) -> None:
        self.trans_mode = trans_mode
        self.length = length
        self.cycle_time = cycle_time
        self.max_delay = max_delay
        type(self).last_mode = self.trans_mode


class _FakeNotificationConnection:
    instances: list[_FakeNotificationConnection] = []

    def __init__(
        self,
        net_id: object,
        port: int,
        host: str,
    ) -> None:
        del net_id, port, host
        self.is_open = False
        self.callback: Callable[..., None] | None = None
        type(self).instances.append(self)

    def set_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def notification(
        self,
        datatype: object,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        del datatype

        def decorator(
            callback: Callable[..., None],
        ) -> Callable[..., None]:
            self.callback = callback
            return callback

        return decorator

    def add_device_notification(
        self,
        address: tuple[int, int],
        attr: object,
        callback: Callable[..., None],
    ) -> tuple[object, object]:
        del attr
        self.callback = callback
        return ((address, "handle"), None)

    def del_device_notification(
        self,
        handle: object,
        user_handle: object,
    ) -> None:
        del handle, user_handle


class _FakeNotificationPyads:
    ADSTRANS_SERVERCYCLE = 3
    PLCTYPE_REAL = object()
    Connection = _FakeNotificationConnection
    NotificationAttrib = _FakeNotificationAttrib


@pytest.mark.asyncio
async def test_ads_driver_exposes_notification_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _point(
        "speed",
        ext=_point_ext(
            index_group=0x4020,
            index_offset=10,
        ),
    )
    table = PointTable(
        PointTableId("ads_pt"),
        Protocol("ads"),
        {"speed": point},
    )
    driver = ADSDriver(
        _endpoint(),
        table,
        {},
    )
    driver._connected = True

    _FakeNotificationConnection.instances = []
    monkeypatch.setattr(
        subscription_module,
        "pyads",
        _FakeNotificationPyads,
    )
    received: list[ProtocolSample] = []
    delivered = asyncio.Event()

    async def callback(sample: ProtocolSample) -> None:
        received.append(sample)
        delivered.set()

    handle = await driver.subscribe(
        (point.point_id,),
        callback,
        interval=0.25,
    )
    connection = _FakeNotificationConnection.instances[0]
    assert _FakeNotificationAttrib.last_mode == _FakeNotificationPyads.ADSTRANS_SERVERCYCLE
    assert connection.callback is not None

    connection.callback(
        None,
        (0x4020, 10),
        None,
        12.5,
    )
    await asyncio.wait_for(delivered.wait(), timeout=1.0)

    assert received[0].point_id == "speed"
    assert received[0].value == 12.5

    await handle.close()
    assert connection.is_open is False


@pytest.mark.asyncio
async def test_ads_subscription_ignores_old_generation_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _point(
        "speed",
        ext=_point_ext(index_group=0x4020, index_offset=10),
    )
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {"speed": point}),
        {},
    )
    driver._connected = True
    _FakeNotificationConnection.instances = []
    monkeypatch.setattr(subscription_module, "pyads", _FakeNotificationPyads)

    received: list[ProtocolSample] = []

    async def on_sample(sample: ProtocolSample) -> None:
        received.append(sample)

    handle = await driver.subscribe(("speed",), on_sample, interval=0.25)
    subscription = handle._subscription
    old_connection = _FakeNotificationConnection.instances[-1]
    old_callback = old_connection.callback
    assert old_callback is not None

    async with subscription._lock:
        await subscription._rebuild_locked()

    new_connection = _FakeNotificationConnection.instances[-1]
    assert new_connection is not old_connection
    assert new_connection.callback is not None

    # A callback from a closed ADS connection must not refresh the new watchdog.
    previous = subscription._last_notification["speed"]
    old_callback(None, (0x4020, 10), None, 1.0)
    await asyncio.sleep(0)
    assert subscription._last_notification["speed"] == previous
    assert received == []

    new_connection.callback(None, (0x4020, 10), None, 2.0)
    for _ in range(10):
        await asyncio.sleep(0)
        if received:
            break
    assert received and received[0].value == 2.0
    await handle.close()


@pytest.mark.asyncio
async def test_ads_subscription_can_close_itself_from_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _point(
        "speed",
        ext=_point_ext(
            index_group=0x4020,
            index_offset=10,
        ),
    )
    table = PointTable(
        PointTableId("ads_pt"),
        Protocol("ads"),
        {"speed": point},
    )
    driver = ADSDriver(
        _endpoint(),
        table,
        {},
    )
    driver._connected = True

    _FakeNotificationConnection.instances = []
    monkeypatch.setattr(
        subscription_module,
        "pyads",
        _FakeNotificationPyads,
    )
    done = asyncio.Event()
    handle_box: list[_Closable] = []

    async def callback(sample: ProtocolSample) -> None:
        del sample
        handle = handle_box[0]
        await handle.close()
        done.set()

    handle = await driver.subscribe(
        (point.point_id,),
        callback,
        interval=0.25,
    )
    handle_box.append(handle)
    connection = _FakeNotificationConnection.instances[0]
    assert connection.callback is not None
    connection.callback(None, (0x4020, 10), None, 12.5)

    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert connection.is_open is False


@pytest.mark.asyncio
async def test_ads_route_added_only_when_normal_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.protocol.ads.config import ADSConfig

    class FakeConnection:
        def __init__(self, reachable: bool) -> None:
            self.is_open = False
            self.reachable = reachable

        def open(self) -> None:
            self.is_open = True

        def read_state(self) -> tuple[int, int]:
            if not self.reachable:
                raise ConnectionError("route missing")
            return (5, 0)

        def close(self) -> None:
            self.is_open = False

    class FakeAds:
        def __init__(self) -> None:
            self.route_added = False
            self.route_calls = 0

        def add_route_to_plc(self, *_args: object) -> bool:
            self.route_calls += 1
            self.route_added = True
            return True

    fake_ads = FakeAds()
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {},
    )
    driver._config = ADSConfig(
        host="192.0.2.20",
        target_net_id="192.0.2.20.1.1",
        target_port=801,
        route_auto_add=True,
    )
    monkeypatch.setattr(driver_module, "pyads", fake_ads)
    monkeypatch.setattr(
        driver_module,
        "_LOCAL_AMS",
        ADSLocalConfig("192.0.2.10.1.1", "192.0.2.10", "user", "private-password"),
    )
    monkeypatch.setattr(
        driver,
        "_new_connection",
        lambda: FakeConnection(fake_ads.route_added),
    )
    await driver.connect()
    assert driver.is_open()
    assert fake_ads.route_calls == 1
    await driver.close()
