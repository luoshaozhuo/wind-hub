"""Positive and negative tests for the historical BUSINESS projection.

Covers exact PointTable matching, one-to-many BusinessPoint mapping, scale/
offset engineering conversion, Units standardization, integer fidelity,
int64/uint64 boundaries, large-integer rejection, non-finite rejection, NULL,
unknown points, missing mappings and fail-closed behaviour (no PROTOCOL fallback).
"""

from __future__ import annotations

import pytest

from core.domain import BusinessPoint, Point, PointTable
from core.domain.identities import BusinessPointId
from core.domain.unit import DIMENSIONLESS, Quantity, Unit
from core.domain.value_objects import DataType, PointAccess, Protocol
from sink_service.application.business_projection import (
    BusinessValue,
    HistoricalPointTable,
)
from sink_service.domain.config import ConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.contract]

POWER = Quantity("active_power")
KILOWATT = Unit("kilowatt", "kW", POWER)
# Same quantity, explicit conversion factors: 1 scaled unit == 1000 base units.
SCALED_POWER = Unit("scaled_power", "sp", POWER, scale_to_base=1000.0)
COUNT = Unit("count", "#", DIMENSIONLESS)
SCALED_COUNT = Unit("thousand_count", "k#", DIMENSIONLESS, scale_to_base=1000.0)


def _point(
    point_id: str,
    business_id: str,
    *,
    source_unit: Unit = KILOWATT,
    scale: float = 1.0,
    offset: float = 0.0,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(business_id),
        source_unit=source_unit,
        access=PointAccess.READ,
        scale=scale,
        offset=offset,
    )


def _business(
    business_id: str,
    data_type: DataType = DataType.FLOAT64,
    standard_unit: Unit = KILOWATT,
) -> BusinessPoint:
    return BusinessPoint(BusinessPointId(business_id), data_type, standard_unit)


def _table(
    points: dict[str, Point],
    businesses: dict[str, BusinessPoint],
    *,
    point_table_id: str = "point_table_modbus_wtg_sim_v1.0.0",
    device_type: str = "wind_turbine",
) -> HistoricalPointTable:
    return HistoricalPointTable(
        point_table_id=point_table_id,
        device_type=device_type,
        manifest_sha256="a" * 64,
        table=PointTable(point_table_id, Protocol("modbus"), points),
        business_points={BusinessPointId(key): value for key, value in businesses.items()},
    )


def test_exact_point_match_projects_to_pinned_business_point() -> None:
    table = _table(
        {"active_power": _point("active_power", "power")},
        {"power": _business("power")},
    )
    result = table.project("active_power", 1500)
    assert isinstance(result, BusinessValue)
    assert result.business_point_id == "power"
    assert result.unit == "kilowatt"
    assert result.value == 1500.0


def test_one_business_point_serves_many_source_points() -> None:
    points = {
        "power_a": _point("power_a", "shared_power"),
        "power_b": _point("power_b", "shared_power"),
    }
    table = _table(points, {"shared_power": _business("shared_power")})
    first = table.project("power_a", 1.0)
    second = table.project("power_b", 2.0)
    assert first.business_point_id == second.business_point_id == "shared_power"
    assert first.value == 1.0 and second.value == 2.0


def test_scale_and_offset_engineering_conversion() -> None:
    point = _point("scaled", "power", scale=2.0, offset=10.0)
    table = _table({"scaled": point}, {"power": _business("power")})
    # engineering = raw * scale + offset = 5 * 2 + 10 = 20
    assert table.project("scaled", 5).value == 20.0


def test_units_standardization_converts_to_business_standard_unit() -> None:
    # Source unit maps 1 -> 1000 base units; the BusinessPoint standard unit
    # is the base unit (scale 1), so 3 source units become 3000 standard units.
    point = _point("power_scaled", "power", source_unit=SCALED_POWER)
    table = _table(
        {"power_scaled": point},
        {"power": _business("power", standard_unit=KILOWATT)},
    )
    assert table.project("power_scaled", 3).value == pytest.approx(3000.0)


def test_combined_scale_offset_and_unit_conversion() -> None:
    # engineering = raw * 2 + 10 in source units, then scaled to the base unit.
    point = _point(
        "power_combined",
        "power",
        source_unit=SCALED_POWER,
        scale=2.0,
        offset=10.0,
    )
    table = _table(
        {"power_combined": point},
        {"power": _business("power", standard_unit=KILOWATT)},
    )
    # engineering = 1 * 2 + 10 = 12 scaled units; 12 * 1000 = 12000 base.
    assert table.project("power_combined", 1).value == pytest.approx(12000.0)


def test_integer_value_is_preserved_without_float_round_trip() -> None:
    point = _point("counter", "count", source_unit=COUNT)
    table = _table(
        {"counter": point},
        {"count": _business("count", DataType.INT64, COUNT)},
    )
    result = table.project("counter", 123456789012)
    assert result.value == 123456789012
    assert isinstance(result.value, int)


def test_int64_boundary_values_are_accepted() -> None:
    point = _point("counter", "count", source_unit=COUNT)
    table = _table(
        {"counter": point},
        {"count": _business("count", DataType.INT64, COUNT)},
    )
    minimum = -(2**63)
    maximum = 2**63 - 1
    assert table.project("counter", minimum).value == minimum
    assert table.project("counter", maximum).value == maximum


def test_uint64_full_range_is_accepted() -> None:
    point = _point("counter", "count", source_unit=COUNT)
    table = _table(
        {"counter": point},
        {"count": _business("count", DataType.UINT64, COUNT)},
    )
    maximum = 2**64 - 1
    assert table.project("counter", maximum).value == maximum
    with pytest.raises(ConfigurationError):
        table.project("counter", -1)


def test_integer_above_lossless_float_threshold_is_rejected() -> None:
    # A scaled source forces the float conversion branch where integers beyond
    # the 2^53 safe-integer threshold cannot be converted losslessly.
    point = _point("counter", "count", source_unit=SCALED_COUNT)
    table = _table(
        {"counter": point},
        {"count": _business("count", DataType.FLOAT64, COUNT)},
    )
    with pytest.raises(ConfigurationError, match="losslessly|integer"):
        table.project("counter", 2**53 + 1)


def test_non_finite_engineering_value_is_rejected() -> None:
    point = _point("bad", "power", scale=2.0)
    table = _table({"bad": point}, {"power": _business("power")})
    with pytest.raises(ConfigurationError):
        table.project("bad", float("inf"))
    with pytest.raises(ConfigurationError):
        table.project("bad", float("nan"))


def test_null_value_returns_null_business_value_without_conversion() -> None:
    point = _point("maybe", "power", scale=1000.0)
    table = _table({"maybe": point}, {"power": _business("power")})
    result = table.project("maybe", None)
    assert result.value is None
    assert result.business_point_id == "power"
    assert result.unit == "kilowatt"


def test_unknown_point_fails_closed() -> None:
    table = _table({"known": _point("known", "power")}, {"power": _business("power")})
    with pytest.raises(ConfigurationError, match="unknown point"):
        table.project("missing", 1.0)


def test_missing_business_mapping_fails_closed() -> None:
    # Point references a BusinessPoint id that is absent from the pinned set.
    point = _point("orphan", "not_present")
    table = _table({"orphan": point}, {"power": _business("power")})
    with pytest.raises(ConfigurationError, match="missing pinned BusinessPoint"):
        table.project("orphan", 1.0)


def test_boolean_is_not_treated_as_integer() -> None:
    point = _point("flag", "flag", source_unit=COUNT)
    table = _table(
        {"flag": point},
        {"flag": _business("flag", DataType.BOOL, COUNT)},
    )
    result = table.project("flag", True)
    assert result.value is True and isinstance(result.value, bool)


def test_type_conflict_fails_closed_without_protocol_fallback() -> None:
    # INT32 BusinessPoint rejects a float that is not integral.
    point = _point("i32", "i32_point", source_unit=COUNT)
    table = _table(
        {"i32": point},
        {"i32_point": _business("i32_point", DataType.INT32, COUNT)},
    )
    with pytest.raises(ConfigurationError):
        table.project("i32", 1.5)
    # Out-of-range integer also fails closed rather than degrading to PROTOCOL.
    with pytest.raises(ConfigurationError):
        table.project("i32", 2**31)
