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
    PointAccess,
    PointTable,
    Protocol as DeviceProtocol,
    Point,
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


def _endpoint() -> ConnectionEndpoint:
    return ConnectionEndpoint("192.0.2.30")


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ,
    ext: dict[str, str | int] | None = None,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=access,
        ext={} if ext is None else ext,
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
    config = parse_iec104_config(_endpoint(), {})

    assert config.port == 2404
    assert config.common_addr == 1
    assert config.k == 12
    assert config.w == 8

    with pytest.raises(ConfigError, match="must not exceed k"):
        parse_iec104_config(
            _endpoint(),
            {"k": 4, "w": 8},
        )


def test_iec104_point_maps_ioa_and_type_id() -> None:
    mapped = parse_iec104_point(
        _point(
            "active_power",
            ext=_point_options(
                ioa=1001,
                type_id="M_ME_NC_1",
            ),
        )
    )

    assert mapped.ioa == 1001
    assert mapped.type_id == "M_ME_NC_1"


def test_iec104_index_rejects_duplicate_ioa() -> None:
    first = _point("p1", ext=_point_options(ioa=100))
    second = _point("p2", ext=_point_options(ioa=100))

    with pytest.raises(ConfigError, match="duplicate IOA"):
        build_iec104_index([first, second])


def test_iec104_writable_point_requires_command_type() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_options(ioa=2001),
    )
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="requires type_id"):
        IEC104Driver(
            _endpoint(),
            table,
            {},
        )


def test_iec104_driver_builds_without_importing_c104() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_options(
            ioa=2001,
            type_id="C_SE_NC_1",
        ),
    )
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    driver = IEC104Driver(
        _endpoint(),
        table,
        {},
    )

    assert driver.health().healthy is False


def test_iec104_writable_point_rejects_monitoring_type() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_options(
            ioa=2001,
            type_id="M_ME_NC_1",
        ),
    )
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="unsupported command type"):
        IEC104Driver(
            _endpoint(),
            table,
            {},
        )


def _driver_for_monitoring_point() -> tuple[IEC104Driver, Point]:
    point = _point("power", ext=_point_options(ioa=100))
    table = PointTable(
        "iec_pt",
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )
    driver = IEC104Driver(
        _endpoint(),
        table,
        {},
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
