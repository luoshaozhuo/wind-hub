from __future__ import annotations

from typing import Any

import pytest

from core.application import ConfigError
from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
)
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.ads import (
    ADSDriver,
    parse_ads_config,
    parse_ads_point,
)
from tests.support.units import NONE_UNIT


def _endpoint(
    *,
    port: int | None = 801,
) -> ConnectionEndpoint:
    return ConnectionEndpoint("192.0.2.20", port)


def _device_options(**options: Any) -> dict[str, Any]:
    return dict(options)


def _point(
    point_id: str,
    *,
    ext: dict[str, Any] | None = None,
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
    **options: Any,
) -> dict[str, Any]:
    return {"data_type": data_type, **options}


def test_ads_config_parses_device_options() -> None:
    config = parse_ads_config(
        _endpoint(),
        _device_options(
            target_net_id="192.0.2.20.1.1",
            timeout=2.5,
        ),
    )

    assert config.host == "192.0.2.20"
    assert config.target_port == 801
    assert config.target_net_id == "192.0.2.20.1.1"
    assert config.timeout == 2.5


def test_ads_config_uses_project_default_port_by_twincat_version() -> None:
    assert parse_ads_config(_endpoint(port=None), {}).target_port == 801
    assert (
        parse_ads_config(
            _endpoint(port=None),
            {"twincat_version": "3"},
        ).target_port
        == 851
    )


def test_ads_config_rejects_unknown_options() -> None:
    try:
        parse_ads_config(
            _endpoint(),
            {"unknown_option": True},
        )
    except ConfigError as exc:
        assert "unknown ADS options" in str(exc)
    else:
        raise AssertionError("unknown ADS option must fail")


def test_ads_symbol_point_requires_session_resolution() -> None:
    mapped = parse_ads_point(
        _point("speed", ext=_point_ext(symbol="MAIN.speed")),
    )

    assert mapped.symbol == "MAIN.speed"
    assert mapped.address_resolved is False
    assert mapped.index_group is None
    assert mapped.index_offset is None
    with pytest.raises(ConfigError, match="unresolved"):
        _ = mapped.group
    assert mapped.data_type == "REAL"
    assert mapped.size == 4


def test_ads_symbol_index_hint_is_not_trusted_before_session_resolution() -> None:
    mapped = parse_ads_point(
        _point(
            "speed",
            ext=_point_ext(
                symbol="MAIN.speed",
                index_group=0x4020,
                index_offset=0,
            ),
        ),
    )
    assert mapped.index_group is None
    assert mapped.index_offset is None
    assert not mapped.address_resolved
    resolved = mapped.resolved(index_group=0x4020, index_offset=0)
    assert resolved.address_resolved
    assert resolved.unresolved().index_group is None


def test_ads_incomplete_index_address_is_rejected() -> None:
    for options in (
        {"index_group": 0},
        {"index_offset": 0},
        {"symbol": "MAIN.speed", "index_group": 0},
        {"symbol": "MAIN.speed", "index_offset": 0},
    ):
        with pytest.raises(ConfigError, match="configured together"):
            parse_ads_point(_point("bad", ext=_point_ext(**options)))


def test_ads_index_zero_is_valid_address() -> None:
    point = parse_ads_point(
        _point("zero", ext=_point_ext(index_group=0, index_offset=0))
    )
    assert point.address_resolved
    assert (point.group, point.offset) == (0, 0)


def test_ads_index_point_is_resolved_without_network() -> None:
    mapped = parse_ads_point(
        _point(
            "power",
            ext=_point_ext(
                data_type="DINT",
                index_group=0x4020,
                index_offset=100,
            ),
        ),
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
            ext=_point_ext(
                data_type="LINT",
                index_group=0x4020,
                index_offset=0,
            ),
        ),
    )

    assert mapped.data_type == "LINT"
    assert mapped.size == 8


def test_ads_driver_precompiles_point_table_without_network_io() -> None:
    point = _point("speed", ext=_point_ext(symbol="MAIN.speed"))
    table = PointTable(PointTableId("ads_pt"), Protocol("ads"), {"speed": point})

    driver = ADSDriver(
        _endpoint(),
        table,
        {},
    )

    assert driver.is_open() is False


def test_ads_symbol_index_address_rejects_invalid_values() -> None:
    from types import SimpleNamespace

    from core.infrastructure.protocol.ads.mapping import symbol_index_address

    assert symbol_index_address(
        "MAIN.speed", SimpleNamespace(index_group=0x4020, index_offset=0)
    ) == (0x4020, 0)
    for group, offset in [(True, 1), (-1, 1), (1, None), (1, False)]:
        with pytest.raises(ConfigError, match="invalid index address"):
            symbol_index_address(
                "MAIN.speed",
                SimpleNamespace(index_group=group, index_offset=offset),
            )


def test_ads_string_requires_explicit_positive_size() -> None:
    for size in (None, 0):
        options: dict[str, Any] = {"symbol": "MAIN.name"}
        if size is not None:
            options["size"] = size
        with pytest.raises(ConfigError, match="STRING requires positive size"):
            parse_ads_point(_point("name", ext=_point_ext(data_type="STRING", **options)))

    point = parse_ads_point(
        _point("name", ext=_point_ext(data_type="STRING", symbol="MAIN.name", size=81))
    )
    assert point.size == 81


def test_ads_symbol_resolution_preserves_configured_size() -> None:
    point = parse_ads_point(
        _point("power", ext=_point_ext(data_type="REAL", symbol="MAIN.power"))
    )
    resolved = point.resolved(index_group=0x4020, index_offset=0)
    assert resolved.size == 4
    assert resolved.address_resolved
