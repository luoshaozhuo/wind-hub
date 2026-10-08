"""新 Collector build_sink_registry 与 Kafka/DB Sink 构造校验单元测试。

Kafka/DB Sink 不建立真实连接，仅验证构造期参数校验与注册表装配。
"""

from __future__ import annotations

import pytest

from collector.application.sinks import (
    DatabaseSinkConnection,
    FileSinkConnection,
    KafkaSinkConnection,
    ResolvedSinkConfig,
    SinkConfig,
)
from collector.infrastructure.sink import build_sink_registry
from collector.infrastructure.sink.db.postgres import DBSink
from collector.infrastructure.sink.file.csv import FileSink
from collector.infrastructure.sink.mq.kafka import KafkaSink
from core.application import ConfigError

# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_registers_builtin_types() -> None:
    registry = build_sink_registry()
    assert registry.registered_types() == ("db", "file", "iec104", "kafka", "modbus")


def test_registry_creates_file_sink(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = build_sink_registry()
    sink = registry.create(
        ResolvedSinkConfig(
            name="f",
            type="file",
            connection=FileSinkConnection(path=str(tmp_path / "x.jsonl")),
            points=[],
        )
    )
    assert isinstance(sink, FileSink)


def test_registry_unknown_type_rejected() -> None:
    registry = build_sink_registry()
    with pytest.raises(ConfigError, match="Unknown sink type"):
        registry.create(
            ResolvedSinkConfig.model_construct(
                name="x",
                type="opcua",
                connection=FileSinkConnection(path="/tmp/x"),
                points=[],
            )
        )


# ---------------------------------------------------------------------------
# KafkaSink 构造校验
# ---------------------------------------------------------------------------


def test_kafka_sink_requires_kafka_connection() -> None:
    with pytest.raises(ConfigError, match="KafkaSinkConnection"):
        KafkaSink(
            SinkConfig.model_construct(
                name="k",
                type="kafka",
                connection=FileSinkConnection(path="/tmp/x.jsonl"),
                points=[],
            )
        )


def test_kafka_sink_valid_connection_constructs() -> None:
    sink = KafkaSink(
        SinkConfig(
            name="k",
            type="kafka",
            connection=KafkaSinkConnection(
                bootstrap_servers="localhost:9092",
                topic="points",
                key_field="device_id",
            ),
            points=[],
        )
    )
    assert sink.health().healthy  # 未投递过，初始 healthy


async def test_kafka_write_before_open_raises() -> None:
    from collector.application.errors import SinkError
    from collector.domain.point_value import PointValue

    sink = KafkaSink(
        SinkConfig(
            name="k",
            type="kafka",
            connection=KafkaSinkConnection(bootstrap_servers="localhost:9092", topic="points"),
            points=[],
        )
    )
    with pytest.raises(SinkError, match="before open"):
        await sink.write([PointValue(device_id="d", point_id="p", value=1.0)])


# ---------------------------------------------------------------------------
# DBSink 构造校验
# ---------------------------------------------------------------------------


def test_db_sink_requires_db_connection() -> None:
    with pytest.raises(ConfigError, match="DatabaseSinkConnection"):
        DBSink(
            SinkConfig.model_construct(
                name="d",
                type="db",
                connection=FileSinkConnection(path="/tmp/x.jsonl"),
                points=[],
            )
        )


def test_db_sink_table_name_whitelist() -> None:
    with pytest.raises(ConfigError, match="table"):
        DBSink(
            SinkConfig(
                name="d",
                type="db",
                connection=DatabaseSinkConnection(
                    dsn="postgresql://u:p@localhost/db",
                    table="t; DROP TABLE x",
                ),
                points=[],
            )
        )


def test_db_sink_insert_sql_and_row() -> None:
    from datetime import UTC, datetime

    from collector.domain.point_value import PointValue

    sink = DBSink(
        SinkConfig(
            name="d",
            type="db",
            connection=DatabaseSinkConnection(
                dsn="postgresql://u:p@localhost/db",
                table="points",
                create_table=True,
            ),
            points=[],
        )
    )
    assert (
        sink._insert_sql()
        == (  # noqa: SLF001
            "INSERT INTO points (device_id, point_id, value, quality, timestamp, source) "
            "VALUES ($1, $2, $3::jsonb, $4, $5, $6)"
        )
    )
    ts = datetime(2026, 10, 3, tzinfo=UTC)
    row = sink._row(  # noqa: SLF001
        PointValue(
            device_id="d",
            point_id="p",
            value=1.5,
            timestamp=ts,
            source="modbus",
        )
    )
    assert row == ("d", "p", "1.5", "good", ts, "modbus")
