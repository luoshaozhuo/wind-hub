"""Physical wide-query projections never merge versioned fields or quality bits."""

from contextlib import contextmanager
from threading import RLock
from unittest.mock import Mock

import pytest

from storage_service.application.wide_query import WideHistoryFilter
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineBackend,
    TDengineSchemaConflictError,
)


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(
            PhysicalPoint("wind_speed", "FLOAT"),
            PhysicalPoint("power", "INT"),
        ),
        max_row_bytes=65536,
    )


def _filter() -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm",
        device_id="wtg001",
        device_config_id="device_wtg_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=0,
        end_time_us=1000,
        point_ids=("wind_speed",),
        limit=10,
    )


def test_reject_unknown_field_without_querying() -> None:
    query = _filter()
    invalid = WideHistoryFilter(
        wind_farm_id=query.wind_farm_id,
        device_id=query.device_id,
        device_config_id=query.device_config_id,
        data_level=query.data_level,
        start_time_us=query.start_time_us,
        end_time_us=query.end_time_us,
        point_ids=("different_version_field",),
        limit=query.limit,
    )
    with pytest.raises(ValueError, match="outside the exact physical schema"):
        invalid.validate(_schema(), maximum=1000)


def test_readback_decodes_quality_by_full_schema_ordinal() -> None:
    schema = _schema()
    backend = object.__new__(TDengineBackend)
    backend._lock = RLock()
    target = f"{schema.database_name('farm')}.{schema.subtable_name('wtg001')}"
    backend._verified_tables = {target}
    backend._wide_unknown_tables = set()
    connection = Mock()
    # Physical order is power, wind_speed. Bits: power BAD=2, wind GOOD=1 -> 0x06.
    connection.query.return_value = [(250, 8.25, b"\x06")]

    @contextmanager
    def borrow(*, lease: object):
        yield connection, Mock()

    backend._borrow_connection = borrow
    lease = Mock()
    result = backend._fetch_wide_history_sync(schema=schema, query=_filter(), lease=lease)
    assert len(result) == 1
    assert result[0].sample_time_us == 250
    assert result[0].points[0][0] == "wind_speed"
    assert result[0].points[0][1] == 8.25
    assert int(result[0].points[0][2]) == 1
    connection.query.assert_called_once()
    sql = connection.query.call_args.args[0]
    assert "quality" in sql and "ts >= 0" in sql and "ts < 1000" in sql
    assert schema.columns[1].column_name in sql


def test_block_readback_during_unsettled_device_write() -> None:
    schema = _schema()
    backend = object.__new__(TDengineBackend)
    backend._lock = RLock()
    target = f"{schema.database_name('farm')}.{schema.subtable_name('wtg001')}"
    backend._verified_tables = {target}
    backend._wide_unknown_tables = {target}
    with pytest.raises(TDengineSchemaConflictError, match="unresolved"):
        backend._fetch_wide_history_sync(schema=schema, query=_filter(), lease=Mock())
