"""Dynamic multi-subtable STMT2 binding and prepared statement ownership."""

import json
from unittest.mock import Mock

import pytest

from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.wide_stmt2 import (
    bind_wide_columns,
    write_wide_stmt2,
)


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("a", "DOUBLE"), PhysicalPoint("b", "INT")),
        max_row_bytes=65536,
    )


def _record(schema: WideSchema, second: int, *, device: str = "wtg_001") -> WideRecord:
    payload = {
        "schema_version": 3,
        "wind_farm_id": "farm",
        "device_config_id": "device_v1.0.0",
        "device_id": device,
        "data_level": "protocol",
        "sample_time": f"2026-11-25T12:00:{second:02d}Z",
        "points": {"a": [1.5, 1], "b": [None, 0]},
    }
    return WideRecord.from_kafka_json(
        json.dumps(payload).encode(),
        physical_points=tuple(column.point_id for column in schema.columns),
    )


def _driver() -> Mock:
    driver = Mock()
    driver.micros_timestamps_to_column.side_effect = lambda data: ("ts", data)
    driver.doubles_to_column.side_effect = lambda data: ("double", data)
    driver.ints_to_column.side_effect = lambda data: ("int", data)
    driver.varbinary_to_column.side_effect = lambda data: ("bytes", data)
    driver.varchar_to_tag.side_effect = lambda value: ("varchar", value)
    driver.int_to_tag.side_effect = lambda value: ("int", value)
    driver.stmt2_bind_param_view.side_effect = lambda **kwargs: kwargs
    return driver


def test_stmt2_binds_multiple_devices_with_complete_identity_tags() -> None:
    schema = _schema()
    rows = [
        _record(schema, 0),
        _record(schema, 1),
        _record(schema, 0, device="wtg_002"),
    ]
    connection = Mock()
    driver = _driver()
    statement = connection.stmt2_statement.return_value
    statement.execute.return_value = 3
    write_wide_stmt2(
        connection,
        driver,
        schema=schema,
        records=rows,
        device_types={"wtg_001": "wind", "wtg_002": "wind"},
    )
    statement.prepare.assert_called_once_with(
        f"INSERT INTO ? USING {schema.database_name('farm')}.{schema.supertable_name()} "
        "TAGS (?, ?, ?, ?, ?) VALUES (?, ?, ?, ?)"
    )
    params = statement.bind.call_args.args[0]
    assert len(params) == 2
    assert [x["table_name"] for x in params] == [
        schema.subtable_name("wtg_001"),
        schema.subtable_name("wtg_002"),
    ]
    assert len(params[0]["columns"][0][1]) == 2
    assert len(params[1]["columns"][0][1]) == 1
    assert params[0]["tags"] == [
        ("varchar", "wtg_001"),
        ("varchar", "wind"),
        ("varchar", schema.point_table_id),
        ("int", 2),
        ("varchar", schema.fingerprint),
    ]
    driver.varbinary_to_column.assert_any_call([b"\x01", b"\x01"])
    statement.close.assert_called_once()


def test_prepared_statement_is_reused_without_double_close() -> None:
    schema = _schema()
    statement, connection, driver = Mock(), Mock(), _driver()
    statement.execute.return_value = 1
    for second in (0, 1):
        write_wide_stmt2(
            connection,
            driver,
            schema=schema,
            records=[_record(schema, second)],
            device_types={"wtg_001": "wind"},
            statement=statement,
        )
    statement.prepare.assert_not_called()
    assert statement.execute.call_count == 2
    statement.close.assert_not_called()
    connection.stmt2_statement.assert_not_called()


def test_negative_affected_count_closes_owned_statement() -> None:
    schema = _schema()
    conn = Mock()
    conn.stmt2_statement.return_value.execute.return_value = -1
    with pytest.raises(RuntimeError, match="negative affected"):
        write_wide_stmt2(
            conn,
            _driver(),
            schema=schema,
            records=[_record(schema, 0)],
            device_types={"wtg_001": "wind"},
        )
    conn.stmt2_statement.return_value.close.assert_called_once()


def test_rejects_unverified_device_before_driver_calls() -> None:
    schema = _schema()
    driver = _driver()
    with pytest.raises(ValueError, match="verified device type"):
        write_wide_stmt2(
            Mock(),
            driver,
            schema=schema,
            records=[_record(schema, 0)],
            device_types={},
        )
    driver.micros_timestamps_to_column.assert_not_called()


def test_stmt2_rejects_more_than_1000_rows_before_driver_allocation() -> None:
    schema = _schema()
    row = _record(schema, 0)
    driver = _driver()
    with pytest.raises(ValueError, match="1000 physical rows"):
        bind_wide_columns(schema, (row,) * 1001, driver)
    driver.micros_timestamps_to_column.assert_not_called()


def test_stmt2_exposes_real_stage_durations_without_changing_binding() -> None:
    schema = _schema()
    connection, driver = Mock(), _driver()
    statement = connection.stmt2_statement.return_value
    statement.execute.return_value = 1
    stages: dict[str, float] = {}
    write_wide_stmt2(
        connection,
        driver,
        schema=schema,
        records=[_record(schema, 0)],
        device_types={"wtg_001": "wind"},
        statement=statement,
        stage_seconds=stages,
    )
    assert set(stages) == {"columns", "bind", "execute"}
    assert all(0 <= duration < 10 for duration in stages.values())
    statement.close.assert_not_called()
