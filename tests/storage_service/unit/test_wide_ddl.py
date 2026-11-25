"""Pure wide-table TDengine DDL contract tests."""

import pytest

from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.wide_ddl import (
    stable_ddl,
    subtable_ddl,
)


def _schema(level: WideDataLevel = WideDataLevel.PROTOCOL) -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_example",
        point_table_version="1.2.0",
        data_level=level,
        points=(
            PhysicalPoint("wind_speed", "FLOAT"),
            PhysicalPoint("active_power", "DOUBLE"),
        ),
        max_row_bytes=65536,
    )


def test_stable_has_one_row_timestamp_complete_columns_and_quality() -> None:
    schema = _schema()
    ddl = stable_ddl(schema, database="wh_example")
    assert ddl.startswith(
        f"CREATE STABLE IF NOT EXISTS wh_example.{schema.supertable_name()}"
    )
    assert "ts TIMESTAMP" in ddl
    assert "quality VARBINARY(1)" in ddl
    assert "device_id VARCHAR(128)" in ddl
    for column in schema.columns:
        assert f"{column.column_name} {column.sql_type}" in ddl
    assert "sample_key" not in ddl
    assert "logical_key" not in ddl
    assert "content_sha256" not in ddl


@pytest.mark.parametrize("level, expected", [
    (WideDataLevel.RAW, "1"),
    (WideDataLevel.PROTOCOL, "2"),
    (WideDataLevel.BUSINESS, "3"),
])
def test_subtable_identity_includes_data_level_and_schema_fingerprint(
    level: WideDataLevel, expected: str
) -> None:
    schema = _schema(level)
    ddl = subtable_ddl(
        schema, database="wh_example", device_id="wtg_001", device_type="wind"
    )
    assert f"USING wh_example.{schema.supertable_name()}" in ddl
    assert f"'point_table_example', {expected}, '{schema.fingerprint}'" in ddl


def test_tag_quote_is_escaped_not_interpolated_as_sql() -> None:
    ddl = subtable_ddl(
        _schema(), database="wh_example", device_id="wtg_o'hare",
        device_type="wind"
    )
    assert "'wtg_o''hare'" in ddl


def test_reject_untrusted_database_identifier() -> None:
    with pytest.raises(ValueError, match="identifier"):
        stable_ddl(_schema(), database="farm_a; DROP TABLE x")
