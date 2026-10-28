"""轻量 Redis Sink：基于 asyncio streams 的 RESP2，零新增依赖。"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress

from core.application.protocol_contract import ConnectionHealth
from core.application.sink_contract import ResolvedSink
from sink_service.application.error import SinkError
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import RedisSinkConnection
from sink_service.infrastructure.adapters.redis_record import encode_redis_record

logger = logging.getLogger(__name__)


def _command(*arguments: str) -> bytes:
    """只编码 Redis 数组命令；每个参数按 UTF-8 字节长度计算。"""
    payload = [f"*{len(arguments)}\r\n".encode("ascii")]
    for argument in arguments:
        value = argument.encode("utf-8")
        payload.extend((f"${len(value)}\r\n".encode("ascii"), value, b"\r\n"))
    return b"".join(payload)


#: 只在新采样时间戳 >= 现存值时间戳时覆盖（单 key 原子 CAS）。
#: 多 Collector 并发写同一 key 时，迟到的旧批次不得回滚已落库的新值；
#: 时间戳缺省（null）按空串处理，等同最旧。ISO-8601 UTC（Z）定宽字符串
#: 可直接字典序比较。返回 1=已写入，0=因过期被拒。
_CAS_SCRIPT = (
    "local current=redis.call('GET',KEYS[1]) "
    "if not current then redis.call('SET',KEYS[1],ARGV[1],'PXAT',ARGV[2]); return 1 end "
    "local ct=cjson.decode(current)['timestamp'] "
    "local nt=cjson.decode(ARGV[1])['timestamp'] "
    "if (nt or '')>=(ct or '') then redis.call('SET',KEYS[1],ARGV[1]); return 1 end "
    "return 0"
)


class RedisSink:
    """通过 SET 原子覆盖各点最新值，单连接串行 Pipeline。"""

    def __init__(self, config: ResolvedSink) -> None:
        connection = dict(config.connection)
        self._task_prefixes = dict(config.task_key_prefixes or {})
        self._task_sources = {
            task_id: set(sources)
            for task_id, sources in (config.task_sources or {}).items()
        }
        self._config = RedisSinkConnection.model_validate(connection)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._message: str | None = "not opened"

    async def _response(self) -> str:
        assert self._reader is not None
        line = await asyncio.wait_for(self._reader.readline(), timeout=self._config.timeout)
        if not line or not line.endswith(b"\r\n"):
            raise ConnectionError("Redis connection closed or invalid RESP reply")
        prefix, value = line[:1], line[1:-2].decode("utf-8", errors="replace")
        if prefix == b"-":
            raise SinkError(f"Redis command failed: {value}")
        if prefix != b"+":
            raise SinkError("Redis command returned unexpected RESP type")
        return value

    async def _integer_response(self) -> int:
        """读取 RESP 整数回复（``:n``）；用于 EVAL CAS 的 0/1 判定。"""
        assert self._reader is not None
        line = await asyncio.wait_for(self._reader.readline(), timeout=self._config.timeout)
        if not line or not line.endswith(b"\r\n"):
            raise ConnectionError("Redis connection closed or invalid RESP reply")
        prefix, value = line[:1], line[1:-2].decode("utf-8", errors="replace")
        if prefix == b"-":
            raise SinkError(f"Redis command failed: {value}")
        if prefix != b":":
            raise SinkError("Redis command returned unexpected RESP type")
        return int(value)

    @staticmethod
    def _dropped_non_finite(batch: list[PointValue]) -> list[PointValue]:
        """剔除非有限浮点（NaN/Inf）——strict JSON 无法表示，否则整批失败。"""
        finite: list[PointValue] = []
        for point in batch:
            if isinstance(point.value, float) and not math.isfinite(point.value):
                logger.warning(
                    "drop non-finite value for %s.%s: %r",
                    point.device_id,
                    point.point_id,
                    point.value,
                )
                continue
            finite.append(point)
        return finite

    async def _request(self, *args: str) -> str:
        assert self._writer is not None
        self._writer.write(_command(*args))
        await asyncio.wait_for(self._writer.drain(), timeout=self._config.timeout)
        return await self._response()

    async def _disconnect(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=self._config.timeout)

    async def _connect(self) -> None:
        """建立连接并完成握手；调用方必须持有 ``self._lock``。"""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._config.host, self._config.port),
                timeout=self._config.timeout,
            )
            if self._config.username is not None:
                if self._config.password is None:
                    raise SinkError("Redis ACL username requires password")
                await self._request(
                    "AUTH", self._config.username, self._config.password.get_secret_value()
                )
            elif self._config.password is not None:
                await self._request("AUTH", self._config.password.get_secret_value())
            if self._config.database != 0:
                await self._request("SELECT", str(self._config.database))
            await self._request("PING")
            self._message = None
        except Exception as exc:
            await self._disconnect()
            self._message = str(exc)
            raise SinkError(f"Redis open failed: {exc}") from exc

    async def open(self) -> None:
        async with self._lock:
            if self._writer is not None:
                return
            await self._connect()

    async def write(self, batch: list[PointValue]) -> None:
        await self.write_task("", batch)

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        if not batch:
            return
        if task_id:
            allowed = self._task_sources.get(task_id, set())
            batch = [item for item in batch if (item.device_id, item.point_id) in allowed]
        batch = self._dropped_non_finite(batch)
        if any(point.max_staleness_ms is None for point in batch):
            raise SinkError("Redis realtime write requires max_staleness_ms")
        if not batch:
            return
        async with self._lock:
            if self._writer is None:
                raise SinkError("Redis sink is not connected")
            assert self._writer is not None
            try:
                records = [
                    encode_redis_record(
                        self._config,
                        point,
                        key_prefix=self._task_prefixes.get(task_id, self._config.key_prefix),
                    )
                    for point in batch
                ]
                commands = [
                    _command(
                        "EVAL", _CAS_SCRIPT, "1", record.key, record.payload,
                        str(int(point.timestamp.timestamp() * 1000) + (point.max_staleness_ms or 0)),
                    )
                    for point, record in zip(batch, records, strict=True)
                ]
                self._writer.write(b"".join(commands))
                await asyncio.wait_for(self._writer.drain(), timeout=self._config.timeout)
                for record in records:
                    result = await self._integer_response()
                    if result not in {0, 1}:
                        raise SinkError(f"Redis CAS EVAL returned unexpected result: {result}")
                    if result == 0:
                        logger.debug("stale sample rejected for key %s", record.key)
                self._message = None
            except Exception as exc:
                await self._disconnect()
                self._message = str(exc)
                raise SinkError(f"Redis write failed: {exc}") from exc

    async def flush(self) -> None:
        """SET Pipeline 在 write 返回前收到全部确认，无本地缓冲。"""

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()
            self._message = "closed"

    async def health(self) -> ConnectionHealth:
        """通过真实 PING 检查 Redis 连接，不执行重连。"""
        async with self._lock:
            if self._writer is None:
                return ConnectionHealth(healthy=False, message=self._message or "not connected")
            try:
                response = await self._request("PING")
                if response != "PONG":
                    raise SinkError(f"Redis PING returned {response!r}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._disconnect()
                self._message = str(exc)
                return ConnectionHealth(healthy=False, message=self._message)
            self._message = None
            return ConnectionHealth(healthy=True)
