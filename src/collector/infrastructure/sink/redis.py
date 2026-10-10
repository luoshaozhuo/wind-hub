"""Redis 最新点值 Sink（异步、批量管线写入）。"""

from __future__ import annotations

import asyncio
from typing import Any

from collector.application.errors import SinkError
from core.application.protocol_contract import ConnectionHealth
from core.application.sink_config import RedisSinkConnection, ResolvedSinkConfig
from core.application.redis_sink_record import encode_redis_record
from core.domain.point_value import PointValue


class RedisSink:
    """每个点位一个 Key，JSON 保留 quality/timestamp，写入失败显式抛出。"""

    def __init__(self, config: ResolvedSinkConfig) -> None:
        connection = config.connection
        if not isinstance(connection, RedisSinkConnection):
            raise ValueError("RedisSink requires RedisSinkConnection")
        self._cfg = connection
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._healthy = False
        self._message: str | None = "not connected"

    async def open(self) -> None:
        async with self._lock:
            if self._client is not None:
                return
            client = None
            try:
                from redis.asyncio import Redis

                client = Redis(
                    host=self._cfg.host,
                    port=self._cfg.port,
                    db=self._cfg.database,
                    password=(self._cfg.password.get_secret_value()
                              if self._cfg.password is not None else None),
                    decode_responses=True,
                )
                await client.ping()
            except Exception as exc:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:
                        pass
                self._healthy = False
                self._message = str(exc)
                raise SinkError(f"Redis connection failed: {exc}") from exc
            self._client = client
            self._healthy = True
            self._message = None

    async def write(self, batch: list[PointValue]) -> None:
        if not batch:
            return
        async with self._lock:
            if self._client is None:
                self._healthy = False
                self._message = "not opened"
                raise SinkError("Redis sink is not opened")
            try:
                async with self._client.pipeline(transaction=False) as pipe:
                    for point in batch:
                        record = encode_redis_record(self._cfg, point)
                        pipe.set(record.key, record.payload)
                    await pipe.execute()
            except Exception as exc:
                self._healthy = False
                self._message = str(exc)
                raise SinkError(f"Redis write failed: {exc}") from exc
            self._healthy = True
            self._message = None

    async def flush(self) -> None:
        """Pipeline 在 write 内立即提交，无待冲刷队列。"""

    async def close(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()
            self._healthy = False
            self._message = "closed"

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self._healthy, message=self._message)
