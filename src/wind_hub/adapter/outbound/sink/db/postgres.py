"""PostgreSQL 输出 sink —— 用 asyncpg 连接池把点值批量 INSERT 到关系表。

实现 :class:`~wind_hub.application.port.sink.SinkPort` 的真实数据库走向：把一批
:class:`~wind_hub.domain.model.point.PointValue` 映射为行，经 ``asyncpg``
连接池的 ``executemany`` 批量写入 ``table``；``value`` 列以 JSON 序列化后按
``JSONB`` 落库（决策 5，兼容任意标量/结构化值），``timestamp`` 传 ``datetime``
由驱动编码为 ``TIMESTAMPTZ``。``create_table: true`` 时在 ``open`` 阶段执行
``CREATE TABLE IF NOT EXISTS``（默认表结构见 :data:`_DEFAULT_SCHEMA`）。

参数在**构造时**校验（缺 ``dsn`` / ``table`` 抛
:class:`~wind_hub.domain.model.errors.ConfigError`）；运行时状态由
``asyncio.Lock`` 保护；写入失败抛 :class:`~wind_hub.domain.model.errors.SinkError`，
连续失败达到阈值后 ``health()`` 报告 unhealthy（决策 6/8，复用 FileSink 模式）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

# asyncpg 0.31 未随包发布 py.typed / 类型桩，mypy 会把该模块标记为
# `import-untyped`；此处按可选额外依赖的标准做法显式抑制，避免泄漏 asyncpg
# 类型到对外接口（``self._pool`` 内部按 ``Any`` 处理，公开签名只出现
# PointValue / HealthStatus / SinkError）。
import asyncpg  # type: ignore[import-untyped]

from wind_hub.application.port.sink import SinkPort
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError, SinkError
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus
from wind_hub.infra import metrics

logger = logging.getLogger(__name__)

# 连续写入失败达到该次数后，sink 标记为 unhealthy（决策 8，复用 FileSink 阈值）。
_MAX_CONSECUTIVE_FAILURES = 5

# 默认表结构（决策 5）；``value`` 用 JSONB 兼容任意标量/结构化值。
_DEFAULT_SCHEMA: dict[str, str] = {
    "device_id": "TEXT NOT NULL",
    "point_id": "TEXT NOT NULL",
    "value": "JSONB",
    "quality": "TEXT NOT NULL",
    "timestamp": "TIMESTAMPTZ NOT NULL",
    "source": "TEXT",
}

# INSERT 固定的列序（与 _row 返回的元组一一对应）。
_COLUMNS = ("device_id", "point_id", "value", "quality", "timestamp", "source")

# 表名白名单（决策 4）：表名以 f-string 拼进 SQL、无法参数化，必须限制为
# 普通 SQL 标识符形态，杜绝经配置注入 SQL 片段（如 ``t; DROP TABLE x``）。
_TABLE_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class DBSink(SinkPort):
    """把点值批量写入 PostgreSQL 关系表的输出 sink。

    参数（``SinkConfig.params``）：

    - ``dsn``（必填）：asyncpg 连接串（如 ``postgresql://user:pass@host/db``）。
    - ``table``（必填）：目标表名，必须匹配 ``[a-zA-Z_][a-zA-Z0-9_]*``（防注入）。
    - ``batch_size``：单次 ``executemany`` 的行数，默认 ``1000``。
    - ``create_table``：``open`` 时是否执行 ``CREATE TABLE IF NOT EXISTS``，默认 ``False``。
    - ``schema``：可选 ``{列名: SQL 类型}`` 映射，与默认表结构合并后用于建表；
      仅影响 ``CREATE TABLE``，``INSERT`` 固定写六个标准列。
    - ``pool_min_size`` / ``pool_max_size``：连接池最小/最大连接数，
      默认 ``1`` / ``10``，要求均为正整数且 min <= max（决策 5）。
    """

    def __init__(self, config: SinkConfig) -> None:
        params = config.params

        self._dsn = self._require_str(params, "dsn")
        self._table = self._validate_table(self._require_str(params, "table"))
        self._batch_size = self._positive_int(params.get("batch_size", 1000), "batch_size")
        self._create_table = bool(params.get("create_table", False))
        self._schema = self._build_schema(params.get("schema"))
        self._pool_min_size = self._positive_int(params.get("pool_min_size", 1), "pool_min_size")
        self._pool_max_size = self._positive_int(params.get("pool_max_size", 10), "pool_max_size")
        if self._pool_min_size > self._pool_max_size:
            raise ConfigError(
                f"DBSink 'pool_min_size' ({self._pool_min_size}) must be <= "
                f"'pool_max_size' ({self._pool_max_size})"
            )

        # 运行时状态 —— 由 `asyncio.Lock` 保护；连接池在 open 后创建。
        self._name = config.name
        self._pool: Any = None
        self._lock = asyncio.Lock()
        self._healthy = True
        self._error_message: str | None = None
        self._consecutive_failures = 0

    # -- 参数校验 ---------------------------------------------------------

    @staticmethod
    def _require_str(params: dict[str, Any], field: str) -> str:
        value = params.get(field)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"DBSink '{field}' is required and must be a non-empty string")
        return value

    @staticmethod
    def _positive_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"DBSink '{field}' must be a positive integer, got {value!r}")
        return value

    @staticmethod
    def _validate_table(table: str) -> str:
        """表名白名单校验——表名拼进 SQL 无法参数化，必须限制标识符形态。"""
        if _TABLE_NAME_RE.fullmatch(table) is None:
            raise ConfigError(f"DBSink 'table' must match [a-zA-Z_][a-zA-Z0-9_]*, got {table!r}")
        return table

    @staticmethod
    def _build_schema(raw: object) -> dict[str, str]:
        """``schema`` 参数与默认表结构合并；键值必须都是字符串。"""
        if raw is None:
            return dict(_DEFAULT_SCHEMA)
        if not isinstance(raw, dict):
            raise ConfigError(f"DBSink 'schema' must be a dict of column→type, got {raw!r}")
        merged = dict(_DEFAULT_SCHEMA)
        for name, dtype in raw.items():
            if not isinstance(name, str) or not isinstance(dtype, str):
                raise ConfigError("DBSink 'schema' keys and values must be strings")
            merged[name] = dtype
        return merged

    # -- SinkPort 契约 ----------------------------------------------------

    async def open(self) -> None:
        """建立连接池并按需建表；幂等，已打开时无操作。

        Raises:
            SinkError: 数据库不可达或建表失败时抛出。
        """
        async with self._lock:
            if self._pool is not None:
                return
            pool = None
            try:
                pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._pool_min_size,
                    max_size=self._pool_max_size,
                )
                if self._create_table:
                    await pool.execute(self._create_table_sql())
            except Exception as exc:
                if pool is not None:
                    await pool.close()
                self._record_failure(f"open failed: {exc}")
                raise SinkError(f"DBSink open failed: {exc}") from exc
            self._pool = pool
            self._mark_healthy()

    async def close(self) -> None:
        """关闭连接池；幂等，已关闭时无操作。

        Raises:
            SinkError: 关闭连接池失败时抛出（池仍被释放）。
        """
        async with self._lock:
            if self._pool is None:
                return
            pool = self._pool
            self._pool = None
        try:
            await pool.close()
        except Exception as exc:
            self._record_failure(f"close failed: {exc}")
            raise SinkError(f"DBSink close failed: {exc}") from exc

    async def write(self, batch: list[PointValue]) -> None:
        """把整批点值按 ``batch_size`` 分片批量 INSERT。

        Raises:
            SinkError: 写入失败，或 ``open`` 尚未调用。
        """
        if not batch:
            return
        async with self._lock:
            if self._pool is None:
                raise SinkError("DBSink.write() called before open()")
            try:
                rows = [self._row(pv) for pv in batch]
                sql = self._insert_sql()
                for i in range(0, len(rows), self._batch_size):
                    await self._pool.executemany(sql, rows[i : i + self._batch_size])
            except Exception as exc:
                metrics.sink_write_failures_total.labels(sink_name=self._name).inc()
                self._record_failure(f"write failed: {exc}")
                raise SinkError(f"DBSink write failed: {exc}") from exc
            metrics.sink_writes_total.labels(sink_name=self._name).inc()
            metrics.sink_points_written_total.labels(sink_name=self._name).inc(len(batch))
            self._mark_healthy()

    async def flush(self) -> None:
        """asyncpg 批量 INSERT 即时提交、无应用层缓冲，故 flush 为无操作。"""

    def health(self) -> HealthStatus:
        """返回缓存的健康状态；连续写入失败达到阈值后报告 unhealthy。"""
        return HealthStatus(healthy=self._healthy, message=self._error_message)

    # -- 内部：SQL 与行映射 ----------------------------------------------

    def _row(self, pv: PointValue) -> tuple[object, ...]:
        """把一个点值映射为 INSERT 的一行（列序与 ``_COLUMNS`` 一致）。"""
        return (
            pv.device_id,
            pv.point_id,
            json.dumps(pv.value, ensure_ascii=False),
            pv.quality.value,
            pv.timestamp,
            pv.source,
        )

    def _insert_sql(self) -> str:
        """固定六列的 INSERT；``value`` 以文本 JSON 参数经 ``::jsonb`` 落库。"""
        cols = ", ".join(_COLUMNS)
        # value 列占位符为 $3，显式加 ::jsonb 让文本 JSON 参数被正确解析。
        placeholders = ", ".join(
            "$3::jsonb" if c == "value" else f"${i + 1}" for i, c in enumerate(_COLUMNS)
        )
        return f"INSERT INTO {self._table} ({cols}) VALUES ({placeholders})"

    def _create_table_sql(self) -> str:
        """由（合并后的）schema 生成 ``CREATE TABLE IF NOT EXISTS`` 语句。"""
        cols = ", ".join(f"{name} {dtype}" for name, dtype in self._schema.items())
        return f"CREATE TABLE IF NOT EXISTS {self._table} ({cols})"

    # -- 内部：健康跟踪 ---------------------------------------------------

    def _mark_healthy(self) -> None:
        """成功写入后复位失败计数、恢复健康。"""
        self._consecutive_failures = 0
        self._healthy = True
        self._error_message = None

    def _record_failure(self, message: str) -> None:
        """累计失败次数，达到阈值后标记 unhealthy（决策 8）。"""
        self._consecutive_failures += 1
        self._error_message = message
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._healthy = False
