from __future__ import annotations

import pytest

from core.application import ConfigError, ConnectionEndpoint, DeviceConnection
from core.domain import (
    PointAccess,
    PointTable,
    Protocol,
    ProtocolPoint,
    RawDataType,
    UNIT_CATALOG,
    UnitCode,
)
from core.infrastructure import (
    IEC104Driver,
    build_iec104_index,
    parse_iec104_config,
    parse_iec104_point,
)


def _connection(**options: object) -> DeviceConnection:
    return DeviceConnection(
        "iec-main",
        "rtu01",
        ConnectionEndpoint(
            "192.0.2.30",
            None,
            options,
        ),
    )


def _point(
    point_id: str,
    *,
    ioa: int,
    access: PointAccess = PointAccess.READ,
    type_id: str | None = None,
    raw_type: str = "float32",
) -> ProtocolPoint:
    options: dict[str, str | int] = {"ioa": ioa}
    if type_id is not None:
        options["type_id"] = type_id
    return ProtocolPoint(
        point_id=point_id,
        business_point_id=point_id,
        raw_type=RawDataType(raw_type),
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=access,
        protocol_options=options,
    )


def test_iec104_config_defaults_and_window_validation() -> None:
    config = parse_iec104_config(_connection())

    assert config.port == 2404
    assert config.common_addr == 1
    assert config.k == 12
    assert config.w == 8

    with pytest.raises(ConfigError, match="must not exceed k"):
        parse_iec104_config(_connection(k=4, w=8))


def test_iec104_point_maps_ioa_and_type_id() -> None:
    mapped = parse_iec104_point(
        _point(
            "active_power",
            ioa=1001,
            type_id="M_ME_NC_1",
        )
    )

    assert mapped.ioa == 1001
    assert mapped.type_id == "M_ME_NC_1"


def test_iec104_index_rejects_duplicate_ioa() -> None:
    first = _point("p1", ioa=100)
    second = _point("p2", ioa=100)

    with pytest.raises(ConfigError, match="duplicate IOA"):
        build_iec104_index([first, second])


def test_iec104_writable_point_requires_command_type() -> None:
    point = _point(
        "setpoint",
        ioa=2001,
        access=PointAccess.WRITE,
    )
    table = PointTable(
        "iec_pt",
        Protocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="requires type_id"):
        IEC104Driver(_connection(), table)


def test_iec104_driver_builds_without_importing_c104() -> None:
    point = _point(
        "setpoint",
        ioa=2001,
        access=PointAccess.WRITE,
        type_id="C_SE_NC_1",
    )
    table = PointTable(
        "iec_pt",
        Protocol("iec104"),
        {point.point_id: point},
    )

    driver = IEC104Driver(_connection(), table)

    assert driver.health().healthy is False



def test_iec104_writable_point_rejects_monitoring_type() -> None:
    point = _point(
        "setpoint",
        ioa=2001,
        access=PointAccess.WRITE,
        type_id="M_ME_NC_1",
    )
    table = PointTable(
        "iec_pt",
        Protocol("iec104"),
        {point.point_id: point},
    )

    with pytest.raises(ConfigError, match="unsupported command type"):
        IEC104Driver(_connection(), table)
