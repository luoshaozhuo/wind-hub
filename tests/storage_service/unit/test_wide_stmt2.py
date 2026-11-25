"""STMT2 device-wide binding tests without running a TDengine service."""

import json
from unittest.mock import Mock

import pytest

from storage_service.domain.wide_record import WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.domain.wide_record import WideDataLevel
from storage_service.infrastructure.adapters.outbound.wide_stmt2 import write_wide_stmt2


def _record(schema: WideSchema, second: int) -> WideRecord:
    payload = {
        "schema_version": 3,
        "wind_farm_id": "farm",
        "device_config_id": "device_v1.0.0",
        "device_id": "wtg_001",
        "data_level": "protocol",
        "sample_time": f"2026-11-25T12:00:{second:02d}Z",
        "points": {"a": [1.5, 1], "b": [None, 0]},
    }
    return WideRecord.from_kafka_json(
        json.dumps(payload).encode(), physical_points=tuple(
            column.point_id for column in schema.columns
        )
    )


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("a", "DOUBLE"), PhysicalPoint("b", "INT")),
        max_row_bytes=65536,
    )


def test_stmt2_binds_complete_rows_and_2bit_quality_column() -> None:
    schema = _schema()
    rows = [_record(schema, 0), _record(schema, 1)]
    connection = Mock()
    driver = Mock()
    driver.micros_timestamps_to_column.side_effect = lambda data: ("ts", data)
    driver.doubles_to_column.side_effect = lambda data: ("double", data)
    driver.ints_to_column.side_effect = lambda data: ("int", data)
    driver.varbinary_to_column.side_effect = lambda data: ("bytes", data)
    driver.stmt2_bind_param_view.side_effect = lambda **kwargs: kwargs
    connection.stmt2_statement.return_value.execute.return_value = 2
    write_wide_stmt2(
        connection, driver,
        table_name=f"{schema.database_name('farm')}.{schema.subtable_name('wtg_001')}",
        schema=schema, records=rows,
    )
    driver.varbinary_to_column.assert_called_once_with([b"\x01", b"\x01"])
    driver.ints_to_column.assert_called_once_with([None, None])
    statement = connection.stmt2_statement.return_value
    statement.prepare.assert_called_once_with(
        f"INSERT INTO {schema.database_name('farm')}.{schema.subtable_name('wtg_001')} "
        "VALUES (?, ?, ?, ?)"
    )
    assert statement.bind.call_count == 1
    statement.close.assert_called_once()


def test_negative_affected_row_count_fails_and_closes_statement() -> None:
    schema = _schema()
    conn = Mock()
    conn.stmt2_statement.return_value.execute.return_value = -1
    driver = Mock()
    with pytest.raises(RuntimeError, match="negative affected"):
        write_wide_stmt2(
            conn, driver, table_name="wh_farm.tb_device",
            schema=schema, records=(_record(schema, 0),),
        )
    conn.stmt2_statement.return_value.close.assert_called_once()


def test_refuses_unsafe_table_name() -> None:
    with pytest.raises(ValueError, match="exact device schema identity"):
        write_wide_stmt2(
            Mock(), Mock(), table_name="wh_x;DROP.tb_a",
            schema=_schema(), records=(_record(_schema(), 0),),
        )
