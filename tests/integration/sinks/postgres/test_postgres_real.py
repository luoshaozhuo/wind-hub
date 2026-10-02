"""DBSink × 真实 PostgreSQL 集成测试。

被测组件是 :class:`DBSink`（含真实 asyncpg 连接池，不做任何替换）；
验证侧是**独立** asyncpg 连接的 SELECT——行必须真正落库。
数据库由 ``postgres_service`` fixture 提供（环境变量优先，否则
Docker Compose 自动拉起）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from wind_hub_collector.adapter.outbound.sink.db.postgres import DBSink
from wind_hub_core.config.schema import SinkConfig
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.point import PointValue


def _pv(point_id: str, value: object, device_id: str = "modbus-1") -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        timestamp=datetime(2026, 10, 2, 8, 0, 0, tzinfo=UTC),
        source="integration-test",
    )


def _table() -> str:
    return f"windhub_test_{uuid.uuid4().hex[:12]}"


def _sink(dsn: str, table: str, **params: object) -> DBSink:
    return DBSink(
        SinkConfig(
            name="db",
            type="db",
            params={"dsn": dsn, "table": table, **params},
        )
    )


class TestPostgresWrite:
    async def test_write_persists_rows(self, postgres_service: str) -> None:
        table = _table()
        sink = _sink(postgres_service, table, create_table=True)
        await sink.open()
        try:
            await sink.write(
                [_pv("rotor.speed", 1200.5), _pv("temp.int", 25), _pv("mode", "auto")]
            )
            await sink.flush()

            conn = await asyncpg.connect(postgres_service)
            try:
                rows = await conn.fetch(
                    f"SELECT device_id, point_id, value, quality, source FROM {table}"
                )
            finally:
                await conn.close()
            # asyncpg 默认把 jsonb 返回为 JSON 文本——按 JSON 解码后比较。
            by_point = {r["point_id"]: r for r in rows}
            assert len(rows) == 3
            assert json.loads(by_point["rotor.speed"]["value"]) == 1200.5
            assert json.loads(by_point["temp.int"]["value"]) == 25
            assert json.loads(by_point["mode"]["value"]) == "auto"
            assert by_point["rotor.speed"]["device_id"] == "modbus-1"
            assert by_point["rotor.speed"]["quality"] == "good"
            assert by_point["rotor.speed"]["source"] == "integration-test"
            assert sink.health().healthy is True
        finally:
            await sink.close()
            conn = await asyncpg.connect(postgres_service)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                await conn.close()

    async def test_open_creates_table_when_configured(self, postgres_service: str) -> None:
        table = _table()
        sink = _sink(postgres_service, table, create_table=True)
        await sink.open()
        try:
            conn = await asyncpg.connect(postgres_service)
            try:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = $1)",
                    table,
                )
            finally:
                await conn.close()
            assert exists is True
        finally:
            await sink.close()
            conn = await asyncpg.connect(postgres_service)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                await conn.close()

    async def test_close_is_idempotent(self, postgres_service: str) -> None:
        sink = _sink(postgres_service, _table(), create_table=True)
        await sink.open()
        await sink.close()
        await sink.close()


class TestPostgresConfigValidation:
    def test_missing_dsn_rejected(self) -> None:
        with pytest.raises(ConfigError, match="dsn"):
            DBSink(SinkConfig(name="db", type="db", params={"table": "t"}))

    def test_invalid_table_name_rejected(self, postgres_service: str) -> None:
        with pytest.raises(ConfigError, match="table"):
            _sink(postgres_service, "t; DROP TABLE x")

    def test_pool_min_greater_than_max_rejected(self, postgres_service: str) -> None:
        with pytest.raises(ConfigError, match="pool_min_size"):
            _sink(postgres_service, "t", pool_min_size=5, pool_max_size=2)
