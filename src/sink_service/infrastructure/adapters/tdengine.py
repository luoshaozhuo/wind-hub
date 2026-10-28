"""TDengine 历史时序 Sink：通过 taosAdapter REST SQL 批量写入。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import urllib.request
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

from core.application.protocol_contract import ConnectionHealth
from core.application.sink_contract import ResolvedSink
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import TDengineSinkConnection
from sink_service.application.error import SinkError


def _sql_text(value: str) -> str:
    """限制单引号转义，仅用于 SQL 数据字面量。"""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _timestamp_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _table_name(device_id: str, point_id: str) -> str:
    identity = json.dumps([device_id, point_id], ensure_ascii=False).encode("utf-8")
    return "p_" + hashlib.sha256(identity).hexdigest()[:32]


class TDengineSink:
    """固定超级表 + 每设备测点子表；不在 Collector 中实现重试或文件缓冲。"""

    def __init__(self, config: ResolvedSink) -> None:
        self._config = TDengineSinkConnection.model_validate(dict(config.connection))
        self._task_sources = {
            task_id: set(sources) for task_id, sources in (config.task_sources or {}).items()
        }
        self._url = (
            f"{'https' if self._config.tls else 'http'}://"
            f"{self._config.host}:{self._config.port}/rest/sql/{self._config.database}"
        )
        self._lock = asyncio.Lock()
        self._opened = False
        self._error: str | None = "not opened"

    def _execute(self, sql: str) -> None:
        credentials = f"{self._config.username}:{self._config.password.get_secret_value()}"
        authorization = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            self._url,
            data=sql.encode("utf-8"),
            headers={"Authorization": f"Basic {authorization}", "Content-Type": "text/plain"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self._config.timeout) as response:
                body = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise SinkError(f"TDengine request failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or body.get("code") != 0:
            description = (
                body.get("desc", "invalid response")
                if isinstance(body, dict)
                else "invalid response"
            )
            raise SinkError(f"TDengine SQL rejected: {description}")

    async def _blocking(self, sql: str) -> None:
        task = asyncio.create_task(asyncio.to_thread(self._execute, sql))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    async def open(self) -> None:
        async with self._lock:
            if self._opened:
                return
            name = f"{self._config.database}.{self._config.stable}"
            ddl = (
                f"CREATE STABLE IF NOT EXISTS {name} ("
                "ts TIMESTAMP, sample_id VARCHAR(192) COMPOSITE KEY, "
                "value_json NCHAR(2048), quality NCHAR(32), "
                "receive_time TIMESTAMP, device_time TIMESTAMP, source NCHAR(64)) "
                "TAGS (device_id NCHAR(192), point_id NCHAR(192))"
            )
            try:
                await self._blocking(ddl)
                await self._blocking(f"SELECT sample_id FROM {name} LIMIT 0")
            except Exception as exc:
                self._error = str(exc)
                raise SinkError(f"TDengine open failed: {exc}") from exc
            self._opened = True
            self._error = None

    async def write(self, batch: list[PointValue]) -> None:
        await self.write_task("", batch)

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        if task_id:
            allowed = self._task_sources.get(task_id, set())
            batch = [point for point in batch if (point.device_id, point.point_id) in allowed]
        if not batch:
            return
        async with self._lock:
            if not self._opened:
                raise SinkError("TDengine Sink is not opened")
            stable = f"{self._config.database}.{self._config.stable}"
            statements: list[str] = []
            for point in batch:
                if isinstance(point.value, float) and not math.isfinite(point.value):
                    continue
                raw_value = json.dumps(point.value, ensure_ascii=False, allow_nan=False)
                if len(raw_value) > 2048:
                    raise SinkError("TDengine point JSON value exceeds 2048 characters")
                if len(point.device_id) > 192 or len(point.point_id) > 192:
                    raise SinkError("TDengine device/point ID exceeds 192 characters")
                if not point.sample_id or len(point.sample_id.encode("utf-8")) > 192:
                    raise SinkError("TDengine requires a sample_id of at most 192 UTF-8 bytes")
                receive = (
                    str(_timestamp_ms(point.receive_time))
                    if point.receive_time is not None
                    else "NULL"
                )
                device = (
                    str(_timestamp_ms(point.device_time))
                    if point.device_time is not None
                    else "NULL"
                )
                statements.append(
                    f"{self._config.database}.{_table_name(point.device_id, point.point_id)} "
                    f"USING {stable} TAGS ({_sql_text(point.device_id)}, "
                    f"{_sql_text(point.point_id)}) VALUES ("
                    f"{_timestamp_ms(point.timestamp)}, {_sql_text(point.sample_id)}, "
                    f"{_sql_text(raw_value)}, "
                    f"{_sql_text(point.quality.value)}, {receive}, {device}, "
                    f"{_sql_text(point.source or point.timestamp_source)})"
                )
            if not statements:
                return
            # 控制 HTTP 请求大小；每个分片独立确认，无自动重传。
            try:
                for offset in range(0, len(statements), 200):
                    await self._blocking(
                        "INSERT INTO " + " ".join(statements[offset : offset + 200])
                    )
            except Exception as exc:
                self._error = str(exc)
                raise SinkError(f"TDengine write failed: {exc}") from exc
            self._error = None

    async def diagnostic(self) -> None:
        """只执行服务端查询校验，不向历史超级表插入诊断点。"""
        health = await self.health()
        if not health.healthy:
            raise SinkError(health.message or "TDengine diagnostic query failed")

    async def flush(self) -> None:
        """write 返回前已获得服务器确认，无本地缓冲。"""

    async def close(self) -> None:
        async with self._lock:
            self._opened = False
            self._error = "closed"

    async def health(self) -> ConnectionHealth:
        async with self._lock:
            if not self._opened:
                return ConnectionHealth(healthy=False, message=self._error or "not opened")
            try:
                await self._blocking("SELECT SERVER_VERSION()")
            except Exception as exc:
                self._error = str(exc)
                return ConnectionHealth(healthy=False, message=self._error)
            self._error = None
            return ConnectionHealth(healthy=True)
