"""Unit tests for ADS point mapping and data-type resolution."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.ads.mapping import (
    ADSPoint,
    map_data_type,
    parse_point,
)
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.errors import ConfigError


def _point(
    point_id: str,
    data_type: str,
    index_group: int = 0x4020,
    index_offset: int = 0,
    **extra: object,
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id="test-dev",
        address=PointAddress(index_group=index_group, index_offset=index_offset, **extra),
        data_type=data_type,
    )


# ---------------------------------------------------------------------------
# map_data_type
# ---------------------------------------------------------------------------


class TestMapDataType:
    @pytest.mark.parametrize(
        ("data_type", "size", "ads_name"),
        [
            ("bool", 1, "BOOL"),
            ("int8", 1, "SINT"),
            ("uint8", 1, "USINT"),
            ("int16", 2, "INT"),
            ("uint16", 2, "UINT"),
            ("int32", 4, "DINT"),
            ("uint32", 4, "UDINT"),
            ("float32", 4, "REAL"),
            ("float64", 8, "LREAL"),
            ("str", 0, "STRING"),
        ],
    )
    def test_maps(self, data_type: str, size: int, ads_name: str) -> None:
        assert map_data_type(data_type) == (size, ads_name)

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ConfigError, match="data_type"):
            map_data_type("blob")


# ---------------------------------------------------------------------------
# parse_point
# ---------------------------------------------------------------------------


class TestParsePoint:
    def test_float32_maps_to_real(self) -> None:
        ap = parse_point(_point("speed", "float32"))
        assert ap == ADSPoint(
            point_id="speed",
            index_group=0x4020,
            index_offset=0,
            data_type="REAL",
            size=4,
        )

    def test_bool(self) -> None:
        ap = parse_point(_point("flag", "bool"))
        assert ap.data_type == "BOOL"
        assert ap.size == 1

    def test_explicit_ads_type_override(self) -> None:
        point = PointConfig(
            point_id="s",
            device_id="dev",
            address=PointAddress(index_group=0x4020, index_offset=0, data_type="LREAL"),
            data_type="float32",
        )
        ap = parse_point(point)
        assert ap.data_type == "LREAL"
        assert ap.size == 8

    def test_type_field_override(self) -> None:
        point = PointConfig(
            point_id="s",
            device_id="dev",
            address=PointAddress(index_group=0x4020, index_offset=0, type="REAL"),
            data_type="float32",
        )
        ap = parse_point(point)
        assert ap.data_type == "REAL"

    def test_explicit_size_override(self) -> None:
        ap = parse_point(_point("s", "str", size=64))
        assert ap.data_type == "STRING"
        assert ap.size == 64

    def test_missing_index_group_raises(self) -> None:
        point = PointConfig(
            point_id="s", device_id="dev", address=PointAddress(index_offset=0), data_type="float32"
        )
        with pytest.raises(ConfigError, match="index_group"):
            parse_point(point)

    def test_missing_index_offset_raises(self) -> None:
        point = PointConfig(
            point_id="s",
            device_id="dev",
            address=PointAddress(index_group=0x4020),
            data_type="float32",
        )
        with pytest.raises(ConfigError, match="index_offset"):
            parse_point(point)

    def test_unsupported_ads_type_raises(self) -> None:
        point = PointConfig(
            point_id="s",
            device_id="dev",
            address=PointAddress(index_group=0x4020, index_offset=0, data_type="WSTRING"),
            data_type="float32",
        )
        with pytest.raises(ConfigError, match="data type"):
            parse_point(point)
