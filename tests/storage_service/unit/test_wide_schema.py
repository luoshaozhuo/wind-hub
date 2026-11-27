"""Wide-table schema identity, type-width and deterministic column order."""

import pytest

from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _build(
    points: tuple[PhysicalPoint, ...],
    *,
    version: str = "1.2.0",
    level: WideDataLevel = WideDataLevel.PROTOCOL,
    limit: int = 65536,
) -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg",
        point_table_version=version,
        data_level=level,
        points=points,
        max_row_bytes=limit,
    )


def test_column_order_uses_utf8_identity_not_input_order() -> None:
    a = PhysicalPoint("wind_speed", "FLOAT")
    b = PhysicalPoint("active_power", "INT")
    left = _build((a, b))
    right = _build((b, a))
    assert left == right
    assert tuple(c.point_id for c in left.columns) == ("active_power", "wind_speed")
    assert left.point_ids == ("active_power", "wind_speed")
    assert left.point_ids is left.point_ids
    assert left.quality_bytes == 1
    assert left.columns[0].column_name.endswith("_v1_2_0")
    assert left.fingerprint == right.fingerprint


def test_version_and_type_changes_produce_distinct_table_families() -> None:
    points = (PhysicalPoint("wind_speed", "FLOAT"),)
    base = _build(points)
    changed_version = _build(points, version="1.2.1")
    changed_type = _build((PhysicalPoint("wind_speed", "DOUBLE"),))
    changed_level = _build(points, level=WideDataLevel.BUSINESS)
    assert (
        len(
            {
                base.supertable_name(),
                changed_version.supertable_name(),
                changed_type.supertable_name(),
                changed_level.supertable_name(),
            }
        )
        == 4
    )
    assert base.subtable_name("wtg_001") != base.subtable_name("wtg_002")


def test_wide_schema_500_fields_has_125_quality_bytes() -> None:
    points = tuple(PhysicalPoint(f"p_{i:03d}", "FLOAT") for i in range(500))
    schema = _build(points)
    assert schema.quality_bytes == 125
    assert len(schema.columns) == 500
    assert len(schema.point_ids) == 500
    assert schema.point_ids[0] == "p_000"
    assert schema.point_ids[-1] == "p_499"
    assert schema.declared_row_bytes == 8 + 125 + 2 + 4 * 500


def test_oversize_raw_schema_is_rejected_not_split() -> None:
    points = tuple(PhysicalPoint(f"raw_{i:03d}", "VARBINARY(2048)") for i in range(500))
    with pytest.raises(ValueError, match="exceeds limit"):
        _build(points)


@pytest.mark.parametrize("sql_type", ["TEXT", "VARBINARY", "VARCHAR(-1)", "BIGINT;DROP"])
def test_invalid_sql_type_is_rejected(sql_type: str) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _build((PhysicalPoint("p", sql_type),))
