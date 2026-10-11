from __future__ import annotations

import pytest

from core.application import ConfigError
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
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.iec104 import (
    IEC104Driver,
    build_iec104_index,
    parse_iec104_config,
    parse_iec104_point,
)


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
        business_point_id=BusinessPointId(point_id),
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
        PointTableId("iec_pt"),
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="requires type_id"):
        IEC104Driver(
            _endpoint(),
            table,
            {},
        )


@pytest.mark.asyncio
async def test_iec104_driver_builds_with_required_c104() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_ext(
            ioa=2001,
            type_id="C_SE_NC_1",
        ),
    )
    table = PointTable(
        PointTableId("iec_pt"),
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    driver = IEC104Driver(
        _endpoint(),
        table,
        {},
    )

    assert driver.is_open() is False


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
        PointTableId("iec_pt"),
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="unsupported command type"):
        IEC104Driver(
            _endpoint(),
            table,
            {},
        )


def test_iec104_driver_exposes_infrastructure_capabilities() -> None:
    point = _point("power", ext=_point_ext(ioa=100))
    table = PointTable(
        PointTableId("iec_pt"),
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )
    driver = IEC104Driver(
        _endpoint(),
        table,
        {},
    )

    assert callable(driver.subscribe)
    assert callable(driver.interrogate)
