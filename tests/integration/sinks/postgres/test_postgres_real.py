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
from wind_hub_core.config.sinks import SinkConfig
from wind_hub_core.model.errors import ConfigError, SinkError
from wind_hub_core.model.point import PointValue

# 默认路径由 Docker Compose 拉起真实服务（外部实例经环境变量接管）；
# 服务真实性显式标注，不计入 mock。
pytestmark = [pytest.mark.docker, pytest.mark.real_service]


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
            connection={"dsn": dsn, "table": table, **params},
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


class TestPostgresTimeoutRecovery:
    async def test_write_timeout_then_pool_recovers(self, postgres_service: str) -> None:
        """write 超时 → SinkError → 数据库恢复 → 后续 write 成功且 health 复位。

        用独立连接的 ``LOCK TABLE ... ACCESS EXCLUSIVE`` 制造真实阻塞（而非
        mock asyncpg），触发 ``asyncio.wait_for`` 取消在途 ``executemany``；
        重点验证被取消的 asyncpg operation 不会让连接池永久损坏——锁释放
        （“PostgreSQL 恢复”）后同一 sink 必须能再次写成功。
        """
        table = _table()
        sink = _sink(postgres_service, table, create_table=True, write_timeout=0.3)
        await sink.open()
        blocker = await asyncpg.connect(postgres_service)
        try:
            async with blocker.transaction():
                # 持锁期间 INSERT 必然阻塞；事务退出（提交/回滚）即释放锁。
                await blocker.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")

                with pytest.raises(SinkError, match="timed out"):
                    await sink.write([_pv("rotor.speed", 1.0)])
                # 失败已计数并记录错误；单次失败未达 unhealthy 阈值。
                assert "timed out" in (sink.health().message or "")

            # “PostgreSQL 恢复”：锁已释放，同一连接池必须恢复可用。
            await sink.write([_pv("rotor.speed", 2.0)])
            assert sink.health().healthy is True
            assert sink.health().message is None

            conn = await asyncpg.connect(postgres_service)
            try:
                rows = await conn.fetch(f"SELECT point_id, value FROM {table}")
            finally:
                await conn.close()
            # 被取消的写未落库；恢复后的写真实落库。
            assert len(rows) == 1
            assert json.loads(rows[0]["value"]) == 2.0
        finally:
            await blocker.close()
            await sink.close()
            conn = await asyncpg.connect(postgres_service)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                await conn.close()


class TestPostgresConfigValidation:
    def test_missing_dsn_rejected(self) -> None:
        with pytest.raises(ConfigError, match="dsn"):
            DBSink(SinkConfig(name="db", type="db", connection={"table": "t"}))

    def test_invalid_table_name_rejected(self, postgres_service: str) -> None:
        with pytest.raises(ConfigError, match="table"):
            _sink(postgres_service, "t; DROP TABLE x")

    def test_pool_min_greater_than_max_rejected(self, postgres_service: str) -> None:
        with pytest.raises(ConfigError, match="pool_min_size"):
            _sink(postgres_service, "t", pool_min_size=5, pool_max_size=2)
