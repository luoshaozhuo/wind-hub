"""轻量 Redis Sink：基于 asyncio streams 的 RESP2，零新增依赖。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from collector.application.errors import SinkError
from core.application.protocol_contract import ConnectionHealth
from core.application.redis_sink_record import encode_redis_record
from core.application.sink_config import RedisSinkConnection, ResolvedSinkConfig
from core.domain.point_value import PointValue


def _command(*arguments: str) -> bytes:
    """只编码 Redis 数组命令；每个参数按 UTF-8 字节长度计算。"""
    payload = [f"*{len(arguments)}\r\n".encode("ascii")]
    for argument in arguments:
        value = argument.encode("utf-8")
        payload.extend((f"${len(value)}\r\n".encode("ascii"), value, b"\r\n"))
    return b"".join(payload)


class RedisSink:
    """通过 SET 原子覆盖各点最新值，单连接串行 Pipeline。"""

    def __init__(self, config: ResolvedSinkConfig) -> None:
        if not isinstance(config.connection, RedisSinkConnection):
            raise ValueError("RedisSink requires RedisSinkConnection")
        self._config = config.connection
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._healthy = False
        self._message: str | None = "not opened"

    async def _response(self) -> str:
        assert self._reader is not None
        line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
        if not line or not line.endswith(b"\r\n"):
            raise ConnectionError("Redis connection closed or invalid RESP reply")
        prefix, value = line[:1], line[1:-2].decode("utf-8", errors="replace")
        if prefix == b"-":
            raise SinkError(f"Redis command failed: {value}")
        if prefix != b"+":
            raise SinkError("Redis command returned unexpected RESP type")
        return value

    async def _request(self, *args: str) -> str:
        assert self._writer is not None
        self._writer.write(_command(*args))
        await asyncio.wait_for(self._writer.drain(), timeout=5.0)
        return await self._response()

    async def _disconnect(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
        self._healthy = False

    async def open(self) -> None:
        async with self._lock:
            if self._writer is not None:
                return
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._config.host, self._config.port),
                    timeout=5.0,
                )
                if self._config.password is not None:
                    await self._request("AUTH", self._config.password.get_secret_value())
                if self._config.database != 0:
                    await self._request("SELECT", str(self._config.database))
                await self._request("PING")
                self._healthy, self._message = True, None
            except Exception as exc:
                await self._disconnect()
                self._message = str(exc)
                raise SinkError(f"Redis open failed: {exc}") from exc

    async def write(self, batch: list[PointValue]) -> None:
        if not batch:
            return
        async with self._lock:
            if self._writer is None:
                self._healthy = False
                raise SinkError("Redis sink is not opened")
            try:
                commands = [
                    _command("SET", record.key, record.payload)
                    for point in batch
                    for record in (encode_redis_record(self._config, point),)
                ]
                self._writer.write(b"".join(commands))
                await asyncio.wait_for(self._writer.drain(), timeout=5.0)
                for _ in commands:
                    if await self._response() != "OK":
                        raise SinkError("Redis SET returned non-OK status")
                self._healthy, self._message = True, None
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

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self._healthy, message=self._message)
