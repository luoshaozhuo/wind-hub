from __future__ import annotations

from core.application import ConnectionEndpoint, DeviceConnection, ConfigError
from core.domain import (
    PointAccess,
    PointTable,
    Protocol,
    ProtocolPoint,
    RawDataType,
    UNIT_CATALOG,
    UnitCode,
)
from core.infrastructure import ADSDriver, parse_ads_config, parse_ads_point


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
