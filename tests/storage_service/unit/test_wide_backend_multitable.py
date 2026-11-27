"""Production TDengine multi-device STMT2 connection reuse and UNKNOWN fencing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from storage_service.application.config import load_storage_config
from storage_service.domain.errors import TDengineSchemaConflictError
from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"), PhysicalPoint("speed", "FLOAT")),
        max_row_bytes=65536,
    )


def _record(schema: WideSchema, device: str, second: int) -> WideRecord:
    payload = {
        "schema_version": 3,
        "wind_farm_id": "farm",
        "device_config_id": "device_v1.0.0",
        "device_id": device,
        "data_level": "protocol",
        "sample_time": f"2026-11-25T12:00:{second:02d}Z",
        "points": {"power": [5, 1], "speed": [1.5, 1]},
    }
    return WideRecord.from_kafka_json(
        json.dumps(payload).encode(),
        physical_points=tuple(column.point_id for column in schema.columns),
    )


async def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TDengineBackend, MetadataStore, Mock, Mock]:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    engine = config.tdengine.model_copy(update={"max_connections": 1, "max_inflight_sql": 1})
    config = config.model_copy(update={"tdengine": engine})
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.open()
    driver, connection, statement = Mock(), Mock(), Mock()
    connection.stmt2_statement.return_value = statement
    statement.execute.return_value = 2
    driver.connect.return_value = connection
    driver.stmt2_bind_param_view.side_effect = lambda **kwargs: kwargs
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    monkeypatch.setenv("WIND_HUB_TDENGINE_PASSWORD", "taosdata")
    monkeypatch.setattr(
        "storage_service.infrastructure.adapters.outbound.tdengine_backend.import_module",
        lambda _: cast(Any, driver),
    )
    backend = TDengineBackend(config=config, metadata=metadata)
    await backend.open()
    schema = _schema()
    database = schema.database_name("farm")
    for device in ("wtg001", "wtg002"):
        name = f"{database}.{schema.subtable_name(device)}"
        backend._verified_tables.add(name)
        backend._wide_device_types[name] = "wind"
    return backend, metadata, connection, statement


@pytest.mark.asyncio
async def test_multi_device_statement_prepared_once_per_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, metadata, connection, statement = await _setup(tmp_path, monkeypatch)
    schema = _schema()
    try:
        await backend.insert_wide_batch(
            schema, [_record(schema, "wtg001", 0), _record(schema, "wtg002", 0)]
        )
        await backend.insert_wide_batch(
            schema, [_record(schema, "wtg001", 1), _record(schema, "wtg002", 1)]
        )
        statement.prepare.assert_called_once()
        assert statement.bind.call_count == 2
        assert len(statement.bind.call_args.args[0]) == 2
        assert statement.execute.call_count == 2
        connection.execute.assert_called_once_with(f"USE {schema.database_name('farm')}")
    finally:
        await backend.close()
        metadata.close()
    statement.close.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_multidevice_write_fences_every_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, metadata, connection, statement = await _setup(tmp_path, monkeypatch)
    schema = _schema()
    statement.execute.side_effect = OSError("injected partial-success uncertainty")
    try:
        with pytest.raises(OSError, match="uncertainty"):
            await backend.insert_wide_batch(
                schema, [_record(schema, "wtg001", 0), _record(schema, "wtg002", 0)]
            )
        database = schema.database_name("farm")
        assert backend._wide_unknown_tables == {
            f"{database}.{schema.subtable_name(device)}" for device in ("wtg001", "wtg002")
        }
        with pytest.raises(TDengineSchemaConflictError, match="UNKNOWN"):
            await backend.insert_wide_batch(schema, [_record(schema, "wtg001", 1)])
    finally:
        await backend.close()
        metadata.close()
    statement.close.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.asyncio
async def test_stmt2_stage_summary_is_bounded_to_256_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, metadata, _connection, _statement = await _setup(tmp_path, monkeypatch)
    try:
        with caplog.at_level("INFO"):
            for _ in range(256):
                backend._observe_wide_write(
                    rows=10,
                    devices=2,
                    stages={"prepare": 0.0, "columns": 0.002, "bind": 0.003, "execute": 0.004},
                )
        assert backend._wide_stage_calls == 0
        assert backend._wide_stage_rows == 0
        assert "rows=2560 devices=512" in caplog.text
        assert "columns_mean_ms=2.000" in caplog.text
        assert "bind_mean_ms=3.000" in caplog.text
        assert "execute_mean_ms=4.000" in caplog.text
    finally:
        await backend.close()
        metadata.close()
