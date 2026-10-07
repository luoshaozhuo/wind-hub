from __future__ import annotations

import asyncio
from typing import Protocol

import pytest

from core.application import (
    ConfigError,
    ProtocolSample,
    Quality,
)
from core.domain import (
    ConnectionEndpoint,
    DeviceConnection,
    PointAccess,
    PointTable,
    Protocol as DeviceProtocol,
    ProtocolPoint,
    RawDataType,
    UNIT_CATALOG,
    UnitCode,
)
from core.infrastructure.protocol.iec104 import (
    IEC104Driver,
    build_iec104_index,
    parse_iec104_config,
    parse_iec104_point,
)


class _Closable(Protocol):
    async def close(self) -> None:
        ...


def _connection() -> DeviceConnection:
    return DeviceConnection(
        "iec-main",
        "rtu01",
        ConnectionEndpoint("192.0.2.30"),
    )


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ,
    raw_type: str = "float32",
) -> ProtocolPoint:
    return ProtocolPoint(
        point_id=point_id,
        business_point_id=point_id,
        raw_type=RawDataType(raw_type),
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=access,
    )


def _point_options(
    *,
    ioa: int,
    type_id: str | None = None,
) -> dict[str, str | int]:
    options: dict[str, str | int] = {"ioa": ioa}
    if type_id is not None:
        options["type_id"] = type_id
    return options


def test_iec104_config_defaults_and_window_validation() -> None:
    config = parse_iec104_config(_connection(), {})

    assert config.port == 2404
    assert config.common_addr == 1
    assert config.k == 12
    assert config.w == 8

    with pytest.raises(ConfigError, match="must not exceed k"):
        parse_iec104_config(
            _connection(),
            {"k": 4, "w": 8},
        )


def test_iec104_point_maps_ioa_and_type_id() -> None:
    mapped = parse_iec104_point(
        _point("active_power"),
        _point_options(
            ioa=1001,
            type_id="M_ME_NC_1",
        ),
    )

    assert mapped.ioa == 1001
    assert mapped.type_id == "M_ME_NC_1"


def test_iec104_index_rejects_duplicate_ioa() -> None:
    first = _point("p1")
    second = _point("p2")

    with pytest.raises(ConfigError, match="duplicate IOA"):
        build_iec104_index(
            [first, second],
            {
                "p1": _point_options(ioa=100),
                "p2": _point_options(ioa=100),
            },
        )


def test_iec104_writable_point_requires_command_type() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
    )
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="requires type_id"):
        IEC104Driver(
            _connection(),
            table,
            {},
            {"setpoint": _point_options(ioa=2001)},
        )


def test_iec104_driver_builds_without_importing_c104() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
    )
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    driver = IEC104Driver(
        _connection(),
        table,
        {},
        {
            "setpoint": _point_options(
                ioa=2001,
                type_id="C_SE_NC_1",
            ),
        },
    )

    assert driver.health().healthy is False


def test_iec104_writable_point_rejects_monitoring_type() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
    )
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="unsupported command type"):
        IEC104Driver(
            _connection(),
            table,
            {},
            {
                "setpoint": _point_options(
                    ioa=2001,
                    type_id="M_ME_NC_1",
                ),
            },
        )


def _driver_for_monitoring_point() -> tuple[IEC104Driver, ProtocolPoint]:
    point = _point("power")
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )
    driver = IEC104Driver(
        _connection(),
        table,
        {},
        {"power": _point_options(ioa=100)},
    )
    return driver, point


def test_iec104_driver_exposes_infrastructure_capabilities() -> None:
    driver, _ = _driver_for_monitoring_point()

    assert callable(driver.subscribe)
    assert callable(driver.interrogate)


@pytest.mark.asyncio
async def test_iec104_subscription_close_drains_inflight_callback() -> None:
    driver, point = _driver_for_monitoring_point()
    started = asyncio.Event()
    release = asyncio.Event()
    received: list[float | int | bool | str | None] = []

    async def callback(sample: ProtocolSample) -> None:
        started.set()
        await release.wait()
        received.append(sample.value)

    handle = await driver.subscribe((point,), callback)
    driver._closed = False
    driver._is_open = True
    driver._store_sample(
        100,
        ProtocolSample(
            point_id="power",
            value=42.0,
            quality=Quality.GOOD,
        ),
    )
    await started.wait()

    closer = asyncio.create_task(handle.close())
    await asyncio.sleep(0)
    assert not closer.done()

    release.set()
    await closer
    assert received == [42.0]

    driver._store_sample(
        100,
        ProtocolSample(
            point_id="power",
            value=43.0,
            quality=Quality.GOOD,
        ),
    )
    await asyncio.sleep(0)
    assert received == [42.0]


@pytest.mark.asyncio
async def test_iec104_subscription_can_close_itself_from_callback() -> None:
    driver, point = _driver_for_monitoring_point()
    done = asyncio.Event()
    handle_box: list[_Closable] = []

    async def callback(sample: ProtocolSample) -> None:
        del sample
        await handle_box[0].close()
        done.set()

    handle = await driver.subscribe((point,), callback)
    handle_box.append(handle)
    driver._closed = False
    driver._is_open = True
    driver._store_sample(
        100,
        ProtocolSample(
            point_id="power",
            value=1.0,
            quality=Quality.GOOD,
        ),
    )

    await asyncio.wait_for(done.wait(), timeout=1.0)
