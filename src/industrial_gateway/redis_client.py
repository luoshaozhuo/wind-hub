"""Gateway 专用 Redis RESP2 MGET 客户端：单连接、异步、无新增依赖。"""

from __future__ import annotations

import asyncio
from contextlib import suppress


def _command(*arguments: str) -> bytes:
    parts = [f"*{len(arguments)}\r\n".encode("ascii")]
    for argument in arguments:
        value = argument.encode("utf-8")
        parts.extend((f"${len(value)}\r\n".encode("ascii"), value, b"\r\n"))
    return b"".join(parts)


class RedisBatchReader:
    """单事件循环持有的 Redis 读连接；失败后下次调用重新连接。"""

    def __init__(
        self,
        host: str,
        port: int = 6379,
        *,
        username: str | None = None,
        password: str | None = None,
        database: int = 0,
        timeout: float = 1.0,
    ) -> None:
        if not host or not 1 <= port <= 65535 or not 0 <= database <= 15 or timeout <= 0:
            raise ValueError("invalid Redis connection options")
        if username is not None and password is None:
            raise ValueError("Redis ACL username requires a password")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._database = database
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _line(self) -> bytes:
        if self._reader is None:
            raise ConnectionError("Redis is disconnected")
        line = await self._reader.readline()
        if not line.endswith(b"\r\n"):
            raise ConnectionError("Redis response truncated")
        return line[:-2]

    async def _reply(self) -> str:
        line = await self._line()
        if line.startswith(b"-"):
            raise ConnectionError(f"Redis command error: {line[1:].decode(errors='replace')}")
        if not line.startswith(b"+"):
            raise ConnectionError("unexpected Redis status response")
        return line[1:].decode("utf-8")

    async def _send(self, *arguments: str) -> None:
        if self._writer is None:
            raise ConnectionError("Redis is disconnected")
        self._writer.write(_command(*arguments))
        await self._writer.drain()

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        if self._password is not None:
            if self._username is not None:
                await self._send("AUTH", self._username, self._password)
            else:
                await self._send("AUTH", self._password)
            if await self._reply() != "OK":
                raise ConnectionError("Redis AUTH failed")
        if self._database:
            await self._send("SELECT", str(self._database))
            if await self._reply() != "OK":
                raise ConnectionError("Redis SELECT failed")

    async def _mget(self, keys: list[str]) -> list[bytes | None]:
        await self._send("MGET", *keys)
        header = await self._line()
        if header.startswith(b"-"):
            raise ConnectionError(f"Redis MGET failed: {header[1:].decode(errors='replace')}")
        if not header.startswith(b"*") or int(header[1:]) != len(keys):
            raise ConnectionError("invalid Redis MGET array length")
        if self._reader is None:
            raise ConnectionError("Redis is disconnected")
        values: list[bytes | None] = []
        for _ in keys:
            item = await self._line()
            if not item.startswith(b"$"):
                raise ConnectionError("invalid Redis bulk response")
            size = int(item[1:])
            if size == -1:
                values.append(None)
                continue
            if not 0 <= size <= 1048576:
                raise ConnectionError("invalid Redis bulk size")
            raw = await self._reader.readexactly(size + 2)
            if not raw.endswith(b"\r\n"):
                raise ConnectionError("invalid Redis bulk terminator")
            values.append(raw[:-2])
        return values

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        if not keys:
            return []
        async with self._lock:
            try:
                async with asyncio.timeout(self._timeout):
                    if self._writer is None:
                        await self._connect()
                    return await self._mget(keys)
            except BaseException:
                await self._close()
                raise

    async def _close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=self._timeout)

    async def close(self) -> None:
        async with self._lock:
            await self._close()
