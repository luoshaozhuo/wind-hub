from __future__ import annotations

import pytest

from core.application import ConfigError
from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UNIT_CATALOG,
    UnitCode,
)
from core.infrastructure.protocol.modbus import (
    ModbusDriver,
    group_consecutive_reads,
    parse_modbus_config,
    parse_modbus_point,
)


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ_WRITE,
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
    address: int,
    register_type: str = "holding",
    data_type: str = "float32",
) -> dict[str, str | int]:
    return {
        "register_type": register_type,
        "address": address,
        "data_type": data_type,
    }


def test_modbus_config_uses_device_options() -> None:
    endpoint = ConnectionEndpoint("192.0.2.10", 1502)

    config = parse_modbus_config(
        endpoint,
        {
            "unit_id": 7,
            "timeout": 2.5,
            "word_order": "big_endian",
        },
    )

    assert config.host == "192.0.2.10"
    assert config.port == 1502
    assert config.unit_id == 7
    assert config.timeout == 2.5
    assert config.word_order == "big_endian"


def test_modbus_point_mapping_and_grouping() -> None:
    first = parse_modbus_point(
        _point("p1", ext=_point_ext(address=10)),
        default_word_order="little_endian",
    )
    second = parse_modbus_point(
        _point("p2", ext=_point_ext(address=12)),
        default_word_order="little_endian",
    )
    distant = parse_modbus_point(
        _point("p3", ext=_point_ext(address=100)),
        default_word_order="little_endian",
    )

    groups = group_consecutive_reads([first, second, distant])

    assert [point.point_id for point in groups[0]] == ["p1", "p2"]
    assert [point.point_id for point in groups[1]] == ["p3"]


def test_modbus_mapping_rejects_write_access_on_input_register() -> None:
    point = _point(
        "readonly",
        access=PointAccess.READ_WRITE,
        ext=_point_ext(
            address=1,
            register_type="input",
            data_type="int16",
        ),
    )

    with pytest.raises(ConfigError, match="read-only"):
        parse_modbus_point(
            point,
            default_word_order="little_endian",
        )


def test_modbus_driver_precompiles_resolved_point_table() -> None:
    point = _point("p1", ext=_point_ext(address=10))
    table = PointTable("pt", Protocol("modbus"), {"p1": point})
    endpoint = ConnectionEndpoint("192.0.2.10", 502)

    driver = ModbusDriver(
        endpoint,
        table,
        {},
    )

    assert driver.health().healthy is False


def test_modbus_grouping_uses_bit_limit_separately() -> None:
    first = parse_modbus_point(
        _point(
            "b1",
            ext=_point_ext(
                address=0,
                register_type="coil",
                data_type="bool",
            ),
        ),
        default_word_order="little_endian",
    )
    second = parse_modbus_point(
        _point(
            "b2",
            ext=_point_ext(
                address=1500,
                register_type="coil",
                data_type="bool",
            ),
        ),
        default_word_order="little_endian",
    )

    groups = group_consecutive_reads(
        [first, second],
        max_gap=2000,
    )

    assert len(groups) == 1


def test_modbus_point_rejects_address_span_overflow() -> None:
    point = _point("overflow", ext=_point_ext(address=65535))

    with pytest.raises(ConfigError, match="address span exceeds"):
        parse_modbus_point(
            point,
            default_word_order="little_endian",
        )
