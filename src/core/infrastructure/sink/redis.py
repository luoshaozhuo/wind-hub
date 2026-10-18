"""轻量 Redis Sink：基于 asyncio streams 的 RESP2，零新增依赖。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from core.application.protocol_contract import ConnectionHealth
from core.application.redis_sink_record import encode_redis_record
from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import RedisSinkConnection


def _command(*arguments: str) -> bytes:
    """只编码 Redis 数组命令；每个参数按 UTF-8 字节长度计算。"""
    payload = [f"*{len(arguments)}\r\n".encode("ascii")]
    for argument in arguments:
        value = argument.encode("utf-8")
        payload.extend((f"${len(value)}\r\n".encode("ascii"), value, b"\r\n"))
    return b"".join(payload)


class RedisSink:
    """通过 SET 原子覆盖各点最新值，单连接串行 Pipeline。"""

    def __init__(self, config: Sink) -> None:
        connection = dict(config.connection)
        self._task_prefixes = dict(connection.pop("task_key_prefixes", {}))
        self._task_sources = {
            task_id: {(device, point) for device, point in sources}
            for task_id, sources in connection.pop("task_sources", {}).items()
        }
        self._config = RedisSinkConnection.model_validate(connection)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._healthy = False
        self._message: str | None = "not opened"
        self._closed = False

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
        self._healthy = False

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
            self._healthy, self._message = True, None
        except Exception as exc:
            await self._disconnect()
            self._message = str(exc)
            raise SinkError(f"Redis open failed: {exc}") from exc

    async def open(self) -> None:
        async with self._lock:
            if self._writer is not None:
                return
            self._closed = False
            await self._connect()

    async def write(self, batch: list[PointValue]) -> None:
        await self.write_task("", batch)

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        if not batch:
            return
        if task_id:
            allowed = self._task_sources.get(task_id, set())
            batch = [item for item in batch if (item.device_id, item.point_id) in allowed]
            if not batch:
                return
        async with self._lock:
            if self._writer is None:
                self._healthy = False
                raise SinkError("Redis sink is not connected")
            assert self._writer is not None
            try:
                commands = [
                    _command("SET", record.key, record.payload)
                    for point in batch
                    for record in (
                        encode_redis_record(
                            self._config,
                            point,
                            key_prefix=self._task_prefixes.get(
                                task_id, self._config.key_prefix
                            ),
                        ),
                    )
                ]
                self._writer.write(b"".join(commands))
                await asyncio.wait_for(self._writer.drain(), timeout=self._config.timeout)
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
            self._closed = True
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
            self._healthy, self._message = True, None
            return ConnectionHealth(healthy=True)
