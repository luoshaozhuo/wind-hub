from __future__ import annotations

import asyncio
import struct
import sys
import time
from collections.abc import Callable
from ctypes import c_ubyte
from typing import ClassVar
from typing import Protocol as TypingProtocol

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application import (
    ConfigError,
    ProtocolSample,
    ProtocolWrite,
    WriteOutcome,
)
from core.application.protocol_contract import Quality
from core.application.settings import ADSLocalConfig
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.units import NONE_UNIT


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
        source_unit=NONE_UNIT,
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
    driver._session = driver_module._AdsSession(fake_connection)
    driver._connected = True

    fake_pyads = _FakePyads()
    monkeypatch.setattr(driver_module, "pyads", fake_pyads)

    result = await driver.write_one(ProtocolWrite(point=point.point_id, value=10.0))

    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert fake_connection.writes[0][2] == 10
    assert type(fake_connection.writes[0][2]) is int


@pytest.mark.asyncio
async def test_ads_driver_local_ams_identity_initializes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyads = _FakePyads()
    monkeypatch.setattr(driver_module, "pyads", fake_pyads)
    monkeypatch.setattr(sys, "platform", "linux")
    config = ADSLocalConfig("192.0.2.10.1.2", "192.0.2.10")
    try:
        await ADSDriver.initialize_local(config)
        await ADSDriver.initialize_local(config)
        assert fake_pyads.open_calls == 1
        assert fake_pyads.local_addresses == ["192.0.2.10.1.2"]
        with pytest.raises(ConfigError, match="conflicts"):
            await ADSDriver.initialize_local(ADSLocalConfig("192.0.2.11.1.2", "192.0.2.11"))
    finally:
        await ADSDriver.close_local()
    assert fake_pyads.close_calls == 1


class _RouteProbeConnection:
    attempts = 0
    closed = 0

    def __init__(self, net_id: object, port: int, host: str) -> None:
        del net_id, port, host
        self.is_open = False

    def set_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def open(self) -> None:
        type(self).attempts += 1
        self.is_open = True

    def read_state(self) -> tuple[int, int]:
        if type(self).attempts == 1:
            raise ConnectionError("initial probe failed")
        return 5, 0

    def close(self) -> None:
        type(self).closed += 1
        self.is_open = False


@pytest.mark.asyncio
async def test_ads_connect_probes_then_adds_route_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RouteProbeConnection.attempts = 0
    _RouteProbeConnection.closed = 0
    calls: list[str] = []

    class Pyads:
        Connection = _RouteProbeConnection

        @staticmethod
        def add_route_to_plc(*args: object) -> bool:
            del args
            calls.append("add")
            return True

    monkeypatch.setattr(driver_module, "pyads", Pyads)
    monkeypatch.setattr(driver_module, "_LOCAL_AMS", ADSLocalConfig("192.0.2.10.1.2", "192.0.2.10"))
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {"route_auto_add": True},
    )
    try:
        await driver.connect()
        assert driver.is_open()
        assert _RouteProbeConnection.attempts == 2
        assert _RouteProbeConnection.closed == 1
        assert calls == ["add"]
    finally:
        await driver.close()
    assert _RouteProbeConnection.closed == 2


@pytest.mark.asyncio
async def test_ads_connect_failure_without_route_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RouteProbeConnection.attempts = 0
    _RouteProbeConnection.closed = 0
    monkeypatch.setattr(
        driver_module, "pyads", type("Pyads", (), {"Connection": _RouteProbeConnection})
    )
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {"route_auto_add": False},
    )
    with pytest.raises(Exception, match="ADS connect failed"):
        await driver.connect()
    assert _RouteProbeConnection.attempts == 1
    assert _RouteProbeConnection.closed == 1


class _TimeoutProbeConnection:
    events: list[object] = []
    state: tuple[int, int] | None = (5, 0)

    def __init__(self, net_id: object, port: int, host: str) -> None:
        del net_id, port, host
        self.is_open = False

    def open(self) -> None:
        self.is_open = True
        self.events.append("open")

    def set_timeout(self, timeout_ms: int) -> None:
        assert self.is_open
        self.events.append(("timeout", timeout_ms))

    def read_state(self) -> tuple[int, int] | None:
        self.events.append("read_state")
        return self.state

    def close(self) -> None:
        self.events.append("close")
        self.is_open = False


@pytest.mark.asyncio
async def test_ads_probe_sets_timeout_after_open_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TimeoutProbeConnection.events = []
    _TimeoutProbeConnection.state = (5, 0)
    monkeypatch.setattr(
        driver_module, "pyads", type("Pyads", (), {"Connection": _TimeoutProbeConnection})
    )
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {"timeout": 1.25},
    )
    await driver.connect()
    assert _TimeoutProbeConnection.events[:3] == ["open", ("timeout", 1250), "read_state"]
    await driver.close()
    assert _TimeoutProbeConnection.events[-1] == "close"


@pytest.mark.asyncio
async def test_ads_probe_rejects_none_state_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TimeoutProbeConnection.events = []
    _TimeoutProbeConnection.state = None
    monkeypatch.setattr(
        driver_module, "pyads", type("Pyads", (), {"Connection": _TimeoutProbeConnection})
    )
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {},
    )
    with pytest.raises(Exception, match="ADS connect failed"):
        await driver.connect()
    assert _TimeoutProbeConnection.events == ["open", ("timeout", 5000), "read_state", "close"]
    assert not driver.is_open()


@pytest.mark.asyncio
async def test_ads_health_stop_state_is_unhealthy_without_extra_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TimeoutProbeConnection.events = []
    _TimeoutProbeConnection.state = (6, 0)  # ADSSTATE_STOP
    monkeypatch.setattr(
        driver_module, "pyads", type("Pyads", (), {"Connection": _TimeoutProbeConnection})
    )
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {},
    )
    with pytest.raises(Exception, match="ADS connect failed"):
        await driver.connect()
    assert _TimeoutProbeConnection.events == ["open", ("timeout", 5000), "read_state", "close"]
    assert driver._session is None
    assert not driver.is_open()


class _FakeNotificationAttrib:
    last_mode: ClassVar[int]

    def __init__(
        self,
        length: int,
        *,
        cycle_time: float,
        trans_mode: int,
    ) -> None:
        self.trans_mode = trans_mode
        self.length = length
        self.cycle_time = cycle_time
        type(self).last_mode = self.trans_mode


class _FakeNotificationConnection:
    def __init__(self) -> None:
        self.is_open = True
        self.callbacks: dict[int, Callable[..., None]] = {}
        self.deleted: list[int] = []
        self._next_handle = 1

    def notification(
        self,
        datatype: object,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        del datatype

        def decorator(
            callback: Callable[..., None],
        ) -> Callable[..., None]:
            return callback

        return decorator

    def add_device_notification(
        self,
        address: tuple[int, int],
        attr: object,
        callback: Callable[..., None],
    ) -> tuple[object, object]:
        del address, attr
        handle = self._next_handle
        self._next_handle += 1
        self.callbacks[handle] = callback
        return (handle, None)

    def del_device_notification(
        self,
        handle: object,
        user_handle: object,
    ) -> None:
        del user_handle
        if isinstance(handle, int):
            self.callbacks.pop(handle, None)
            self.deleted.append(handle)

    def fire(self, handle: int, value: float) -> None:
        raw = (c_ubyte * 4)(*struct_pack_real(value))
        self.callbacks[handle](handle, (0x4020, 10), None, raw)


class _FakeNotificationPyads:
    ADSTRANS_SERVERCYCLE = 3
    PLCTYPE_REAL = object()
    NotificationAttrib = _FakeNotificationAttrib


def struct_pack_real(value: float) -> bytes:
    return struct.pack("<f", value)


def _notification_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ADSDriver, _FakeNotificationConnection]:
    point = _point("speed", ext=_point_ext(index_group=0x4020, index_offset=10))
    table = PointTable(PointTableId("ads_pt"), Protocol("ads"), {"speed": point})
    driver = ADSDriver(_endpoint(), table, {})
    connection = _FakeNotificationConnection()
    driver._session = driver_module._AdsSession(connection)
    driver._connected = True
    monkeypatch.setattr(subscription_module, "pyads", _FakeNotificationPyads)
    return driver, connection


@pytest.mark.asyncio
async def test_ads_driver_exposes_notification_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, connection = _notification_driver(monkeypatch)
    received: list[ProtocolSample] = []
    delivered = asyncio.Event()

    async def callback(sample: ProtocolSample) -> None:
        received.append(sample)
        delivered.set()

    handle = await driver.subscribe(("speed",), callback, interval=0.25)
    assert _FakeNotificationAttrib.last_mode == _FakeNotificationPyads.ADSTRANS_SERVERCYCLE
    assert connection.callbacks.keys() == {1}

    connection.fire(1, 12.5)
    await asyncio.wait_for(delivered.wait(), timeout=1.0)

    assert received[0].point_id == "speed"
    assert received[0].value == 12.5

    await handle.close()
    # 订阅句柄关闭只注销 notification，不关闭 Driver 的共享业务连接。
    assert connection.deleted == [1]
    assert connection.is_open
    assert driver._subscriptions == set()


@pytest.mark.asyncio
async def test_ads_subscription_ignores_old_generation_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, connection = _notification_driver(monkeypatch)
    received: list[ProtocolSample] = []

    async def on_sample(sample: ProtocolSample) -> None:
        received.append(sample)

    handle = await driver.subscribe(("speed",), on_sample, interval=0.25)
    old_callback = connection.callbacks[1]
    await handle.close()
    assert connection.deleted == [1]

    # 关闭后在同连接上重新订阅；旧一代原生回调残留触发时必须静默丢弃。
    new_handle = await driver.subscribe(("speed",), on_sample, interval=0.25)
    try:
        old_callback(1, (0x4020, 10), None, (c_ubyte * 4)(*struct_pack_real(1.0)))
        for _ in range(10):
            await asyncio.sleep(0)
        assert received == []

        connection.fire(2, 2.0)
        for _ in range(10):
            await asyncio.sleep(0)
            if received:
                break
        assert len(received) == 1 and received[0].value == 2.0
    finally:
        await new_handle.close()


@pytest.mark.asyncio
async def test_ads_subscription_can_close_itself_from_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, connection = _notification_driver(monkeypatch)
    done = asyncio.Event()
    handle_box: list[_Closable] = []

    async def callback(sample: ProtocolSample) -> None:
        del sample
        await handle_box[0].close()
        done.set()

    handle = await driver.subscribe(("speed",), callback, interval=0.25)
    handle_box.append(handle)
    connection.fire(1, 12.5)

    await asyncio.wait_for(done.wait(), timeout=1.0)
    # Worker 在用户回调内关闭自身订阅：注销成功、不死锁，共享连接保持打开。
    assert connection.deleted == [1]
    assert connection.is_open
    assert driver._subscriptions == set()


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

        def set_timeout(self, timeout_ms: int) -> None:
            del timeout_ms

        def read_state(self) -> tuple[int, int]:
            if not self.reachable:
                raise ConnectionError("route missing")
            return (5, 0)

        def close(self) -> None:
            self.is_open = False

    class FakeAds:
        def __init__(self) -> None:
            self.route_added = False
            self.route_calls: list[tuple[object, ...]] = []

        def add_route_to_plc(self, *args: object) -> bool:
            self.route_calls.append(args)
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
        username="plc-user",
        password="plc-password",
    )
    monkeypatch.setattr(driver_module, "pyads", fake_ads)
    monkeypatch.setattr(
        driver_module,
        "_LOCAL_AMS",
        ADSLocalConfig("192.0.2.10.1.1", "192.0.2.10"),
    )
    monkeypatch.setattr(
        driver,
        "_new_connection",
        lambda: FakeConnection(fake_ads.route_added),
    )
    await driver.connect()
    assert driver.is_open()
    # 本机身份来自 ADSLocalConfig，远程地址与凭据来自设备有效 ADSConfig。
    assert fake_ads.route_calls == [
        ("192.0.2.10.1.1", "192.0.2.10", "192.0.2.20", "plc-user", "plc-password")
    ]
    await driver.close()


@pytest.mark.asyncio
async def test_ads_route_not_added_when_auto_add_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        is_open = False

        def open(self) -> None:
            self.is_open = True

        def read_state(self) -> tuple[int, int]:
            raise ConnectionError("route missing")

        def close(self) -> None:
            self.is_open = False

    class FakeAds:
        route_calls = 0

        def add_route_to_plc(self, *_args: object) -> bool:
            self.route_calls += 1
            return True

    fake_ads = FakeAds()
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {"route_auto_add": False},
    )
    monkeypatch.setattr(driver_module, "pyads", fake_ads)
    monkeypatch.setattr(driver, "_new_connection", FakeConnection)
    with pytest.raises(Exception, match="ADS connect failed"):
        await driver.connect()
    assert fake_ads.route_calls == 0


@pytest.mark.asyncio
async def test_ads_route_not_added_when_connect_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        is_open = False

        def open(self) -> None:
            self.is_open = True

        def set_timeout(self, timeout_ms: int) -> None:
            del timeout_ms

        def read_state(self) -> tuple[int, int]:
            return (5, 0)

        def close(self) -> None:
            self.is_open = False

    class FakeAds:
        route_calls = 0

        def add_route_to_plc(self, *_args: object) -> bool:
            self.route_calls += 1
            return True

    fake_ads = FakeAds()
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {"route_auto_add": True},
    )
    monkeypatch.setattr(driver_module, "pyads", fake_ads)
    monkeypatch.setattr(driver, "_new_connection", FakeConnection)
    await driver.connect()
    assert driver.is_open()
    assert fake_ads.route_calls == 0
    await driver.close()


class _SessionConnection:
    """支持 read_state/read/write/close 的最小会话 fake。"""

    def __init__(self, *, read_delay: float = 0.0, read_raises: bool = False) -> None:
        self.is_open = True
        self.close_calls = 0
        self._read_delay = read_delay
        self._read_raises = read_raises

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def read(self, group: int, offset: int, datatype: object) -> int:
        del group, datatype
        if self._read_delay:
            time.sleep(self._read_delay)
        if self._read_raises or not self.is_open:
            raise ConnectionError("socket reset by peer")
        return offset + 1

    def write(self, *args: object) -> None:
        if not self.is_open:
            raise ConnectionError("closed")

    def close(self) -> None:
        self.is_open = False
        self.close_calls += 1


def _connected_driver(
    connection: _SessionConnection,
) -> ADSDriver:
    point = _point("speed", ext=_point_ext(data_type="DINT", index_group=0x4020, index_offset=10))
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {"speed": point}),
        {},
    )
    driver._session = driver_module._AdsSession(connection)
    driver._connected = True
    return driver


@pytest.mark.asyncio
async def test_close_is_idempotent_and_clears_session() -> None:
    connection = _SessionConnection()
    driver = _connected_driver(connection)

    await driver.close()
    await driver.close()  # 重复关闭不得操作已摘除的连接或报错

    assert connection.close_calls == 1
    assert not driver.is_open()
    assert driver._session is None
    assert driver._subscriptions == set()


@pytest.mark.asyncio
async def test_io_after_close_raises_protocol_error_without_false_success() -> None:
    connection = _SessionConnection()
    driver = _connected_driver(connection)
    await driver.close()

    with pytest.raises(Exception, match="active connection"):
        await driver.read_one("speed")
    with pytest.raises(Exception, match="active connection"):
        await driver.read_many(("speed",))
    with pytest.raises(Exception, match="active connection"):
        await driver.write_one(ProtocolWrite(point="speed", value=1))
    with pytest.raises(Exception, match="active connection"):
        await driver.write_many((ProtocolWrite(point="speed", value=1),))
    assert (await driver.health()).healthy is False
    with pytest.raises(Exception, match="active connection"):
        await driver.subscribe(("speed",), _noop_callback, interval=0.1)


async def _noop_callback(sample: ProtocolSample) -> None:
    del sample


@pytest.mark.asyncio
async def test_subscribe_requires_active_connection() -> None:
    driver = ADSDriver(
        _endpoint(),
        PointTable(PointTableId("ads_pt"), Protocol("ads"), {}),
        {},
    )
    with pytest.raises(Exception, match="active connection"):
        await driver.subscribe((), _noop_callback, interval=0.1)


@pytest.mark.asyncio
async def test_close_during_pending_io_completes_with_bounded_time() -> None:
    connection = _SessionConnection(read_delay=0.2)
    driver = _connected_driver(connection)

    read_task = asyncio.create_task(driver.read_one("speed"))
    await asyncio.sleep(0.05)  # 确保读命令已占用 IO 锁
    # close 等在 IO 锁后：在途读正常完成，close 在有限时间内收尾，不丢结果、不挂死。
    await asyncio.wait_for(driver.close(), timeout=2.0)

    sample = await read_task
    assert sample.quality is Quality.GOOD
    assert sample.value == 11
    assert not driver.is_open()
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_transport_error_keeps_session_open_and_surfaces_failure() -> None:
    connection = _SessionConnection(read_raises=True)
    driver = _connected_driver(connection)

    with pytest.raises(Exception, match="ADS read failed"):
        await driver.read_one("speed")
    # 一次传输失败不等于 TCP 断链：会话保持打开，由上层 Watchdog 决定恢复。
    assert driver.is_open()
    assert connection.is_open
