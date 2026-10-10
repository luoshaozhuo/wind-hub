"""测试侧最小 Redis 客户端——asyncio streams 的 RESP2 只读子集。

只实现测试验证所需的 ``GET``/``PING``；与 RedisSink 的生产实现互相独立
（系统边界验证不复用被测代码路径）。无新增第三方依赖。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress


def point_key(device_id: str, point_id: str, *, prefix: str = "wind-hub") -> str:
    """按生产键规则计算一设备一点的 Redis key（长度前缀防冒号歧义）。

    对应 ``core.application.redis_sink_record.encode_redis_record`` 的对外
    键契约：``{key_prefix}:{len(device)}:{device}:{len(name)}:{name}``。
    """
    return f"{prefix}:{len(device_id)}:{device_id}:{len(point_id)}:{point_id}"


def parse_address(address: str) -> tuple[str, int]:
    """解析 ``host:port`` 形式的 Redis 地址。"""
    host, sep, port = address.rpartition(":")
    if not sep or not host:
        raise ValueError(f"invalid redis address: {address!r}")
    return host, int(port)


def _command(*arguments: str) -> bytes:
    payload = [f"*{len(arguments)}\r\n".encode("ascii")]
    for argument in arguments:
        value = argument.encode("utf-8")
        payload.extend((f"${len(value)}\r\n".encode("ascii"), value, b"\r\n"))
    return b"".join(payload)


class RedisTestClient:
    """单连接串行 RESP2 客户端（仅测试验证用）。"""

    def __init__(self, address: str) -> None:
        self._host, self._port = parse_address(address)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=5.0,
        )

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)

    async def __aenter__(self) -> RedisTestClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _reply(self) -> str | None:
        assert self._reader is not None
        line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
        if not line or not line.endswith(b"\r\n"):
            raise ConnectionError("Redis connection closed or invalid RESP reply")
        prefix, value = line[:1], line[1:-2]
        if prefix == b"-":
            raise RuntimeError(f"Redis command failed: {value.decode(errors='replace')}")
        if prefix == b"+":
            return value.decode("utf-8")
        if prefix == b"$":
            length = int(value)
            if length < 0:
                return None
            blob = await asyncio.wait_for(
                self._reader.readexactly(length + 2), timeout=5.0
            )
            return blob[:-2].decode("utf-8")
        raise RuntimeError(f"Redis command returned unexpected RESP type: {prefix!r}")

    async def _request(self, *args: str) -> str | None:
        assert self._writer is not None
        self._writer.write(_command(*args))
        await asyncio.wait_for(self._writer.drain(), timeout=5.0)
        return await self._reply()

    async def ping(self) -> str:
        reply = await self._request("PING")
        assert reply is not None
        return reply

    async def get(self, key: str) -> str | None:
        """读取 key 的当前值；不存在返回 ``None``。"""
        return await self._request("GET", key)
