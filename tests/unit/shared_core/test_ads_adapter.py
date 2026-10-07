from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
import core.infrastructure.protocol.ads.router as router_module
import core.infrastructure.protocol.ads.subscription as subscription_module


class _Closable(Protocol):
    async def close(self) -> None:
        ...


from core.application import (
    ConfigError,
    ConnectionEndpoint,
    DeviceConnection,
    ProtocolError,
    ProtocolSample,
    ProtocolWrite,
)
from core.application.config import (
    PointAccess,
    PointTable,
    Protocol,
    ProtocolPoint,
    RawDataType,
)
from core.domain import (
    UNIT_CATALOG,
    UnitCode,
)
from core.infrastructure.protocol.ads import (
    ADSDriver,
    ADSLocalConfig,
    ADSLocalRouter,
    parse_ads_config,
    parse_ads_point,
)


def _connection(
    *,
    port: int | None = 801,
    **options: object,
) -> DeviceConnection:
    return DeviceConnection(
        "ads-main",
        "wt01",
        ConnectionEndpoint(
            "192.0.2.20",
            port,
            options,
        ),
    )


def _point(
    point_id: str,
    *,
    raw_type: str = "float32",
    **options: object,
) -> ProtocolPoint:
    return ProtocolPoint(
        point_id=point_id,
        business_point_id=point_id,
        raw_type=RawDataType(raw_type),
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        protocol_options=options,
    )


def test_ads_config_parses_connection_options() -> None:
    config = parse_ads_config(
        _connection(
            target_net_id="192.0.2.20.1.1",
            timeout=2.5,
            read_mode="sequential",
            max_concurrent_reads=8,
        )
    )

    assert config.host == "192.0.2.20"
    assert config.target_port == 801
    assert config.target_net_id == "192.0.2.20.1.1"
    assert config.timeout == 2.5
    assert config.read_mode == "sequential"
    assert config.max_concurrent_reads == 8


def test_ads_config_uses_project_default_port_by_twincat_version() -> None:
    assert parse_ads_config(_connection(port=None)).target_port == 801
    assert (
        parse_ads_config(
            _connection(port=None, twincat_version="3")
        ).target_port
        == 802
    )


def test_ads_config_rejects_unknown_options() -> None:
    try:
        parse_ads_config(_connection(unknown_option=True))
    except ConfigError as exc:
        assert "unknown ADS options" in str(exc)
    else:
        raise AssertionError("unknown ADS option must fail")


def test_ads_symbol_point_requires_session_resolution() -> None:
    mapped = parse_ads_point(
        _point("speed", symbol="MAIN.speed")
    )

    assert mapped.symbol == "MAIN.speed"
    assert mapped.address_resolved is False
    assert mapped.data_type == "REAL"
    assert mapped.size == 4


def test_ads_index_point_is_resolved_without_network() -> None:
    mapped = parse_ads_point(
        _point(
            "power",
            raw_type="int32",
            index_group=0x4020,
            index_offset=100,
        )
    )

    assert mapped.address_resolved is True
    assert mapped.index_group == 0x4020
    assert mapped.index_offset == 100
    assert mapped.data_type == "DINT"
    assert mapped.size == 4


def test_ads_int64_mapping_is_supported() -> None:
    mapped = parse_ads_point(
        _point(
            "counter",
            raw_type="int64",
            index_group=0x4020,
            index_offset=0,
        )
    )

    assert mapped.data_type == "LINT"
    assert mapped.size == 8


def test_ads_driver_precompiles_point_table_without_importing_pyads() -> None:
    point = _point(
        "speed",
        symbol="MAIN.speed",
    )
    table = PointTable("ads_pt", Protocol("ads"), {"speed": point})

    driver = ADSDriver(_connection(), table)

    assert driver.health().healthy is False



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
        self.writes.append(
            (index_group, index_offset, value, datatype)
        )


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
        raw_type="int32",
        index_group=0x4020,
        index_offset=12,
    )
    table = PointTable(
        "ads_pt",
        Protocol("ads"),
        {"setpoint": point},
    )
    driver = ADSDriver(_connection(), table)
    fake_connection = _FakeADSConnection()
    driver._connection = fake_connection
    driver._connected = True

    fake_pyads = _FakePyads()
    monkeypatch.setattr(driver_module, "_pyads", lambda: fake_pyads)

    results = await driver.write(
        (ProtocolWrite(point=point, value=10.0),)
    )

    assert results[0].success is True
    assert fake_connection.writes[0][2] == 10
    assert type(fake_connection.writes[0][2]) is int


@pytest.mark.asyncio
async def test_ads_local_router_initializes_once_and_rejects_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyads = _FakePyads()
    monkeypatch.setattr(router_module, "_pyads", lambda: fake_pyads)

    router = ADSLocalRouter()
    config = ADSLocalConfig("192.0.2.10.1.2", "192.0.2.10")

    await router.initialize(config)
    await router.initialize(config)

    assert fake_pyads.open_calls == 1
    assert fake_pyads.local_addresses == ["192.0.2.10.1.2"]

    with pytest.raises(ConfigError, match="different local identity"):
        await router.initialize(
            ADSLocalConfig("192.0.2.11.1.2", "192.0.2.11")
        )

    await router.close()
    assert fake_pyads.close_calls == 1



class _FakeNotificationAttrib:
    def __init__(
        self,
        length: int,
        *,
        cycle_time: float,
        max_delay: float,
    ) -> None:
        self.length = length
        self.cycle_time = cycle_time
        self.max_delay = max_delay


class _FakeNotificationConnection:
    instances: list["_FakeNotificationConnection"] = []

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
    PLCTYPE_REAL = object()
    Connection = _FakeNotificationConnection
    NotificationAttrib = _FakeNotificationAttrib


@pytest.mark.asyncio
async def test_ads_driver_exposes_notification_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _point(
        "speed",
        index_group=0x4020,
        index_offset=10,
    )
    table = PointTable(
        "ads_pt",
        Protocol("ads"),
        {"speed": point},
    )
    driver = ADSDriver(_connection(), table)
    driver._connected = True

    _FakeNotificationConnection.instances = []
    monkeypatch.setattr(
        subscription_module,
        "_pyads",
        lambda: _FakeNotificationPyads,
    )
    received: list[ProtocolSample] = []

    async def callback(sample: ProtocolSample) -> None:
        received.append(sample)

    handle = await driver.subscribe(
        (point,),
        callback,
        interval=0.25,
    )
    connection = _FakeNotificationConnection.instances[0]
    assert connection.callback is not None

    connection.callback(
        None,
        (0x4020, 10),
        None,
        12.5,
    )
    await asyncio.sleep(0)

    assert received[0].point_id == "speed"
    assert received[0].value == 12.5

    await handle.close()
    assert connection.is_open is False



@pytest.mark.asyncio
async def test_ads_local_router_has_single_process_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyads = _FakePyads()
    monkeypatch.setattr(router_module, "_pyads", lambda: fake_pyads)

    first = ADSLocalRouter()
    second = ADSLocalRouter()
    config = ADSLocalConfig("192.0.2.10.1.2", "192.0.2.10")

    await first.initialize(config)
    with pytest.raises(ConfigError, match="another instance"):
        await second.initialize(config)

    await first.close()
    await second.initialize(config)
    await second.close()

    assert fake_pyads.open_calls == 2
    assert fake_pyads.close_calls == 2



@pytest.mark.asyncio
async def test_ads_subscription_can_close_itself_from_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _point(
        "speed",
        index_group=0x4020,
        index_offset=10,
    )
    table = PointTable(
        "ads_pt",
        Protocol("ads"),
        {"speed": point},
    )
    driver = ADSDriver(_connection(), table)
    driver._connected = True

    _FakeNotificationConnection.instances = []
    monkeypatch.setattr(
        subscription_module,
        "_pyads",
        lambda: _FakeNotificationPyads,
    )
    done = asyncio.Event()
    handle_box: list[_Closable] = []

    async def callback(sample: ProtocolSample) -> None:
        del sample
        handle = handle_box[0]
        await handle.close()
        done.set()

    handle = await driver.subscribe(
        (point,),
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
async def test_ads_local_router_releases_owner_when_pyads_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ADSLocalRouter()
    second = ADSLocalRouter()
    config = ADSLocalConfig("192.0.2.10.1.2", "192.0.2.10")

    def unavailable() -> object:
        raise ProtocolError("pyads unavailable")

    monkeypatch.setattr(router_module, "_pyads", unavailable)
    with pytest.raises(ProtocolError, match="unavailable"):
        await first.initialize(config)

    fake_pyads = _FakePyads()
    monkeypatch.setattr(router_module, "_pyads", lambda: fake_pyads)
    await second.initialize(config)
    await second.close()
