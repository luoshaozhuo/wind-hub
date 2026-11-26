"""Atomic wide-row physical binding, including RAW bytes and SQL bounds."""

import json

import pytest

from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_row import bind_wide_row
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _row(
    *,
    level: WideDataLevel,
    points: dict[str, list[object]],
    types: tuple[PhysicalPoint, ...],
) -> tuple[WideRecord, WideSchema]:
    columns = tuple(point.point_id for point in types)
    schema = WideSchema.build(
        point_table_id="point_table_test",
        point_table_version="1.0.0",
        data_level=level,
        points=types,
        max_row_bytes=65536,
    )
    payload = {
        "schema_version": 3,
        "wind_farm_id": "farm_a",
        "device_config_id": "device_test_v1.0.0",
        "device_id": "wtg_001",
        "data_level": level.value,
        "sample_time": "2026-11-25T12:00:00Z",
        "points": points,
    }
    row = WideRecord.from_kafka_json(json.dumps(payload).encode("utf-8"), physical_points=columns)
    return row, schema


def test_complete_protocol_record_binding_including_null() -> None:
    types = (
        PhysicalPoint("a", "FLOAT"),
        PhysicalPoint("b", "BIGINT"),
        PhysicalPoint("c", "VARCHAR(8)"),
    )
    row, schema = _row(
        level=WideDataLevel.PROTOCOL,
        types=types,
        points={"a": [8.2, 1], "b": [None, 2], "c": ["wind", 1]},
    )
    bound = bind_wide_row(row, schema)
    assert bound.values == (8.2, None, "wind")
    assert bound.quality_bitmap == row.quality_bitmap


def test_raw_hex_is_decoded_into_original_binary() -> None:
    types = (PhysicalPoint("payload", "VARBINARY(4)"),)
    row, schema = _row(
        level=WideDataLevel.RAW,
        types=types,
        points={"payload": ["0102ff", 1]},
    )
    assert bind_wide_row(row, schema).values == (b"\x01\x02\xff",)


@pytest.mark.parametrize(
    "level, sql_type, value, error",
    [
        (WideDataLevel.PROTOCOL, "TINYINT", 128, "outside physical type"),
        (WideDataLevel.PROTOCOL, "BIGINT UNSIGNED", -1, "outside physical type"),
        (WideDataLevel.PROTOCOL, "FLOAT", 1e100, "FLOAT overflow"),
        (WideDataLevel.PROTOCOL, "VARCHAR(2)", "中", "width exceeded"),
        (WideDataLevel.RAW, "VARBINARY(2)", "010203", "width exceeded"),
    ],
)
def test_invalid_value_fails_before_any_io(
    level: WideDataLevel,
    sql_type: str,
    value: object,
    error: str,
) -> None:
    types = (PhysicalPoint("p", sql_type),)
    row, schema = _row(
        level=level,
        types=types,
        points={"p": [value, 1]},
    )
    with pytest.raises(ValueError, match=error):
        bind_wide_row(row, schema)


@pytest.mark.parametrize(
    "sql_type, value",
    [
        ("FLOAT", 2**24 + 1),
        ("DOUBLE", 2**53 + 1),
    ],
)
def test_no_lossy_integer_to_float_conversion(sql_type: str, value: int) -> None:
    row, schema = _row(
        level=WideDataLevel.PROTOCOL,
        types=(PhysicalPoint("p", sql_type),),
        points={"p": [value, 1]},
    )
    with pytest.raises(ValueError, match="represented exactly"):
        bind_wide_row(row, schema)
