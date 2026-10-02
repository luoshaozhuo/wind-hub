"""Unit tests for the real DBSink (asyncpg connection pool mocked).

覆盖：构造期参数校验、``open`` 建立连接池并（按需）建表、幂等 open/close、
行映射（``value`` 以 JSON 文本落库）、``batch_size`` 分片 ``executemany``、
写入失败抛 ``SinkError``、以及连续失败达到阈值后报告 unhealthy。

``asyncpg`` 通过 monkeypatch 替换为内存假实现，不发起任何真实数据库连接。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from wind_hub.adapter.outbound.sink.db.postgres import DBSink
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError, SinkError
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.port.outbound import HealthStatus

_TS = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

_MODULE = "wind_hub.adapter.outbound.sink.db.postgres"

_DEFAULT_CREATE = (
    "CREATE TABLE IF NOT EXISTS t (device_id TEXT NOT NULL, point_id TEXT NOT NULL, "
    "value JSONB, quality TEXT NOT NULL, timestamp TIMESTAMPTZ NOT NULL, source TEXT)"
)
_INSERT = (
    "INSERT INTO t (device_id, point_id, value, quality, timestamp, source) "
    "VALUES ($1, $2, $3::jsonb, $4, $5, $6)"
)


class _FakePool:
    """内存假连接池：记录建表 SQL 与 executemany 调用，可注入执行失败。"""

    instances: list[_FakePool] = []
    fail_executemany = False
    hang_executemany = False

    def __init__(self, dsn: str, **kwargs: Any) -> None:
        self.dsn = dsn
        self.kwargs = kwargs
        self.executed_sql: list[str] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []
        self.closed = False
        _FakePool.instances.append(self)

    async def execute(self, sql: str) -> None:
        self.executed_sql.append(sql)

    async def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        if _FakePool.hang_executemany:
            await asyncio.Event().wait()  # 模拟连接黑洞：永不返回
        if _FakePool.fail_executemany:
            raise OSError("insert failed")
        self.executemany_calls.append((sql, list(rows)))

    async def close(self) -> None:
        self.closed = True


class _FakeAsyncpg:
    """替代 ``asyncpg`` 模块：只暴露本 sink 用到的 ``create_pool``。"""

    def __init__(self) -> None:
        self.fail_connect = False

    async def create_pool(self, dsn: str, min_size: int, max_size: int) -> _FakePool:
        if self.fail_connect:
            raise OSError("connection refused")
        return _FakePool(dsn, min_size=min_size, max_size=max_size)


@pytest.fixture
def fake_asyncpg(monkeypatch: pytest.MonkeyPatch) -> _FakeAsyncpg:
    _FakePool.instances = []
    _FakePool.fail_executemany = False
    _FakePool.hang_executemany = False
    fake = _FakeAsyncpg()
    monkeypatch.setattr(f"{_MODULE}.asyncpg", fake)
    return fake


def _cfg(**params: Any) -> SinkConfig:
    return SinkConfig(
        name="s1",
        type="db",
        params={"dsn": "postgresql://u@h/db", "table": "t", **params},
    )


def _pv(point_id: str = "p1", value: Any = 800.0, device_id: str = "d1") -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        quality=Quality.GOOD,
        timestamp=_TS,
        source="modbus",
    )


# ---------------------------------------------------------------------------
# 构造期参数校验
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_missing_dsn_raises(self) -> None:
        with pytest.raises(ConfigError, match="dsn"):
            DBSink(SinkConfig(name="s1", type="db", params={"table": "t"}))

    def test_missing_table_raises(self) -> None:
        with pytest.raises(ConfigError, match="table"):
            DBSink(SinkConfig(name="s1", type="db", params={"dsn": "postgresql://u@h/db"}))

    def test_invalid_batch_size_raises(self) -> None:
        with pytest.raises(ConfigError, match="batch_size"):
            DBSink(_cfg(batch_size=0))

    def test_invalid_schema_non_dict_raises(self) -> None:
        with pytest.raises(ConfigError, match="schema"):
            DBSink(_cfg(schema="not-a-dict"))

    def test_invalid_schema_value_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="schema"):
            DBSink(_cfg(schema={"value": 123}))

    def test_defaults_applied(self) -> None:
        sink = DBSink(_cfg())
        assert sink._batch_size == 1000
        assert sink._create_table is False

    def test_valid_construction_is_healthy(self, fake_asyncpg: None) -> None:
        assert DBSink(_cfg()).health() == HealthStatus(healthy=True)


# ---------------------------------------------------------------------------
# SQL 生成
# ---------------------------------------------------------------------------


class TestSql:
    def test_default_create_table_sql(self, fake_asyncpg: None) -> None:
        assert DBSink(_cfg())._create_table_sql() == _DEFAULT_CREATE

    def test_insert_sql_casts_value_to_jsonb(self, fake_asyncpg: None) -> None:
        assert DBSink(_cfg())._insert_sql() == _INSERT

    def test_schema_merges_over_default(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg(schema={"value": "TEXT", "extra": "INTEGER"}))
        sql = sink._create_table_sql()
        assert "value TEXT" in sql
        assert "extra INTEGER" in sql
        assert "value JSONB" not in sql


# ---------------------------------------------------------------------------
# open / close 生命周期
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_creates_pool(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        assert len(_FakePool.instances) == 1
        assert _FakePool.instances[0].dsn == "postgresql://u@h/db"
        assert _FakePool.instances[0].executed_sql == []  # create_table 默认关闭
        await sink.close()

    async def test_open_with_create_table_runs_ddl(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg(create_table=True))
        await sink.open()
        assert _FakePool.instances[0].executed_sql == [_DEFAULT_CREATE]
        await sink.close()

    async def test_open_idempotent(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        await sink.open()
        assert len(_FakePool.instances) == 1
        await sink.close()

    async def test_open_failure_raises_sink_error(self, fake_asyncpg: _FakeAsyncpg) -> None:
        fake_asyncpg.fail_connect = True
        sink = DBSink(_cfg())
        with pytest.raises(SinkError, match="open failed"):
            await sink.open()

    async def test_close_closes_pool(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        await sink.close()
        assert _FakePool.instances[0].closed is True
        await sink.close()  # 重复 close 幂等

    async def test_write_before_open_raises(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        with pytest.raises(SinkError, match="before open"):
            await sink.write([_pv()])


# ---------------------------------------------------------------------------
# 行映射与写入
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_maps_row_with_json_value(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        await sink.write([_pv(value=800.0)])
        await sink.close()

        pool = _FakePool.instances[0]
        assert len(pool.executemany_calls) == 1
        sql, rows = pool.executemany_calls[0]
        assert sql == _INSERT
        assert rows[0] == ("d1", "p1", "800.0", "good", _TS, "modbus")

    async def test_none_value_serializes_to_json_null(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        await sink.write([_pv(value=None)])
        assert _FakePool.instances[0].executemany_calls[0][1][0][2] == "null"
        await sink.close()

    async def test_batch_size_chunks_executemany(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg(batch_size=2))
        await sink.open()
        await sink.write([_pv(point_id=f"p{i}") for i in range(5)])
        await sink.close()

        pool = _FakePool.instances[0]
        assert [len(rows) for _, rows in pool.executemany_calls] == [2, 2, 1]

    async def test_empty_batch_is_noop(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        await sink.write([])
        assert _FakePool.instances[0].executemany_calls == []
        await sink.close()

    async def test_flush_is_noop(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        await sink.flush()  # asyncpg 无应用层缓冲，flush 应为无操作且不抛异常
        await sink.close()



# ---------------------------------------------------------------------------
# 健康状态
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_single_failure_keeps_healthy(self, fake_asyncpg: None) -> None:
        _FakePool.fail_executemany = True
        sink = DBSink(_cfg())
        await sink.open()
        with pytest.raises(SinkError):
            await sink.write([_pv()])
        assert sink.health().healthy is True
        await sink.close()

    async def test_repeated_failures_mark_unhealthy(self, fake_asyncpg: None) -> None:
        _FakePool.fail_executemany = True
        sink = DBSink(_cfg())
        await sink.open()
        for _ in range(5):
            with pytest.raises(SinkError):
                await sink.write([_pv()])
        assert sink.health().healthy is False
        assert "write failed" in (sink.health().message or "")
        await sink.close()

    async def test_success_after_failures_recovers(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        _FakePool.fail_executemany = True
        for _ in range(4):
            with pytest.raises(SinkError):
                await sink.write([_pv()])
        assert sink.health().healthy is True
        _FakePool.fail_executemany = False
        await sink.write([_pv()])
        assert sink.health().healthy is True
        await sink.close()

    async def test_hanging_write_times_out_and_records_failure(
        self, fake_asyncpg: None
    ) -> None:
        """连接黑洞回归：executemany 永不返回时 write 必须在 write_timeout
        内失败并计入健康跟踪——否则 sink 消费者卡死、健康状态永不翻转。"""
        _FakePool.hang_executemany = True
        sink = DBSink(_cfg(write_timeout=0.2))
        await sink.open()
        started = time.monotonic()
        with pytest.raises(SinkError, match="write failed"):
            await sink.write([_pv()])
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"write hung for {elapsed:.1f}s despite write_timeout"
        assert sink._consecutive_failures == 1  # noqa: SLF001
        await sink.close()


# ---------------------------------------------------------------------------
# write_timeout 参数
# ---------------------------------------------------------------------------


class TestWriteTimeout:
    def test_default_write_timeout(self) -> None:
        sink = DBSink(_cfg())
        assert sink._write_timeout == 10.0  # noqa: SLF001

    @pytest.mark.parametrize("bad", [0, -1, "10", None, True])
    def test_invalid_write_timeout_raises(self, bad: object) -> None:
        with pytest.raises(ConfigError, match="write_timeout"):
            DBSink(_cfg(write_timeout=bad))


# ---------------------------------------------------------------------------
# 表名白名单（决策 4）
# ---------------------------------------------------------------------------


class TestTableNameValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "t; DROP TABLE users",
            "1abc",  # 数字开头
            "my-table",  # 含连字符
            "my table",  # 含空格
            'x"injection',  # 含引号
            "schema.table",  # 含点号（不在白名单内）
        ],
    )
    def test_invalid_table_name_raises(self, bad: str) -> None:
        with pytest.raises(ConfigError, match="table"):
            DBSink(_cfg(table=bad))

    @pytest.mark.parametrize("good", ["t", "points", "_raw", "T1", "a_b_c_9"])
    def test_valid_table_name_accepted(self, good: str, fake_asyncpg: None) -> None:
        assert DBSink(_cfg(table=good))._table == good  # noqa: SLF001


# ---------------------------------------------------------------------------
# 连接池参数（决策 5）
# ---------------------------------------------------------------------------


class TestPoolParams:
    def test_pool_size_defaults(self) -> None:
        sink = DBSink(_cfg())
        assert sink._pool_min_size == 1  # noqa: SLF001
        assert sink._pool_max_size == 10  # noqa: SLF001

    def test_pool_min_greater_than_max_raises(self) -> None:
        with pytest.raises(ConfigError, match="pool_min_size"):
            DBSink(_cfg(pool_min_size=20, pool_max_size=10))

    @pytest.mark.parametrize("field", ["pool_min_size", "pool_max_size"])
    def test_non_positive_pool_size_raises(self, field: str) -> None:
        with pytest.raises(ConfigError, match=field):
            DBSink(_cfg(**{field: 0}))

    async def test_open_passes_configured_pool_sizes(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg(pool_min_size=2, pool_max_size=5))
        await sink.open()
        assert _FakePool.instances[0].kwargs["min_size"] == 2
        assert _FakePool.instances[0].kwargs["max_size"] == 5
        await sink.close()

    async def test_open_uses_default_pool_sizes(self, fake_asyncpg: None) -> None:
        sink = DBSink(_cfg())
        await sink.open()
        assert _FakePool.instances[0].kwargs["min_size"] == 1
        assert _FakePool.instances[0].kwargs["max_size"] == 10
        await sink.close()
