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
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    UnitCode,
)
from core.domain import (
    Protocol as DeviceProtocol,
)
from core.infrastructure.protocol.iec104 import (
    IEC104Driver,
    build_iec104_index,
    parse_iec104_config,
    parse_iec104_point,
)


class _Closable(Protocol):
    async def close(self) -> None: ...


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


def _point_ext(
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
            ext=_point_ext(
                ioa=1001,
                type_id="M_ME_NC_1",
            ),
        )
    )

    assert mapped.ioa == 1001
    assert mapped.type_id == "M_ME_NC_1"


def test_iec104_index_rejects_duplicate_ioa() -> None:
    first = _point("p1", ext=_point_ext(ioa=100))
    second = _point("p2", ext=_point_ext(ioa=100))

    with pytest.raises(ConfigError, match="duplicate IOA"):
        build_iec104_index([first, second])


def test_iec104_writable_point_requires_command_type() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_ext(ioa=2001),
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
        ext=_point_ext(
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
        ext=_point_ext(
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
    point = _point("power", ext=_point_ext(ioa=100))
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

    handle = await driver.subscribe((point.point_id,), callback)
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

    handle = await driver.subscribe((point.point_id,), callback)
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


def _monitoring_table(point_id: str = "power", *, ioa: int = 1001) -> PointTable:
    point = _point(point_id, ext=_point_ext(ioa=ioa, type_id="M_ME_NC_1"))
    return PointTable("iec_pt", DeviceProtocol("iec104"), {point.point_id: point})


def test_iec104_update_point_table_clears_sample_mirror() -> None:
    """切换映射后样本镜像必须清空：样本以 IOA 为键，IOA 复用会把旧点身份错配给新点。"""
    driver = IEC104Driver(_endpoint(), _monitoring_table(), {})
    driver._samples[1001] = ProtocolSample(point_id="power", value=1.0)

    driver.update_point_table(_monitoring_table("power_v2"))

    assert driver._samples == {}
    assert "power_v2" in driver._points_by_id


def test_iec104_update_point_table_rejects_invalid_write_and_keeps_old_mapping() -> None:
    """先校验后切换：新表命令类型非法时保留旧映射与样本。"""
    driver = IEC104Driver(_endpoint(), _monitoring_table(), {})
    old_index = driver._points_by_ioa
    bad_point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_ext(ioa=2001, type_id="M_ME_NC_1"),
    )
    bad_table = PointTable("iec_pt", DeviceProtocol("iec104"), {bad_point.point_id: bad_point})

    with pytest.raises(ConfigError, match="unsupported command type"):
        driver.update_point_table(bad_table)

    assert driver._points_by_ioa is old_index
    assert "power" in driver._points_by_id
