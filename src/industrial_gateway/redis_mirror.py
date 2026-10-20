"""Redis Sink 最新值批量读取：严格解码，失败时不写入陈旧数据。"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Protocol
from dataclasses import dataclass
from datetime import UTC, datetime

from .domain import Scalar
from .point_store import PointStore


@dataclass(frozen=True, slots=True)
class RedisPointSource:
    device_id: str
    point_id: str
    key_prefix: str
    max_age_seconds: float = 3.0
    target_device_id: str | None = None
    target_point_id: str | None = None

    def __post_init__(self) -> None:
        if not self.device_id or not self.point_id or not self.key_prefix:
            raise ValueError("Redis source identifiers must not be empty")
        if self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

    @property
    def target(self) -> tuple[str, str]:
        return (self.target_device_id or self.device_id, self.target_point_id or self.point_id)

    @property
    def key(self) -> str:
        device, point = self.device_id, self.point_id
        return f"{self.key_prefix}:{len(device)}:{device}:{len(point)}:{point}"


def decode_record(raw: bytes | None, source: RedisPointSource, now: datetime) -> Scalar:
    """仅接受符合 Sink 契约、质量有效且时间新鲜的数值。"""
    if raw is None:
        raise ValueError(f"missing Redis key: {source.key}")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("quality") != "good":
            raise ValueError("Redis sample quality is not GOOD")
        stamp = data["timestamp"]
        if not isinstance(stamp, str):
            raise ValueError("missing sample timestamp")
        timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("sample timestamp must have timezone")
        age = (now - timestamp.astimezone(UTC)).total_seconds()
        if age < -1 or age > source.max_age_seconds:
            raise ValueError("sample is stale or in the future")
        value = data["value"]
        if type(value) not in (bool, int, float):
            raise ValueError("sample value must be a scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("sample value is not finite")
        return value
    except (json.JSONDecodeError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"invalid Redis record for {source.key}") from exc


class RedisBatchClient(Protocol):
    def mget(self, keys: list[str]) -> Awaitable[list[bytes | None]]: ...


class RedisMirror:
    """一次 MGET 更新所有已配置反馈点。失效点不再允许作为有效读数。"""

    def __init__(self, store: PointStore, sources: list[RedisPointSource]) -> None:
        if len({s.target for s in sources}) != len(sources):
            raise ValueError("duplicate Redis feedback source")
        self._store = store
        self._sources = list(sources)
        self._healthy: dict[tuple[str, str], bool] = {
            s.target: False for s in sources
        }

    def healthy(self, device_id: str, point_id: str) -> bool:
        return self._healthy.get((device_id, point_id), False)

    async def refresh(self, client: RedisBatchClient) -> None:
        """Redis client 提供异步 mget；采用注入接口以便复用现有连接客户端。"""
        try:
            records = await client.mget([s.key for s in self._sources])
        except Exception:
            for source in self._sources:
                key = source.target
                self._healthy[key] = False
                await self._store.invalidate_feedback(*key)
            raise
        if len(records) != len(self._sources):
            for source in self._sources:
                key = source.target
                self._healthy[key] = False
                await self._store.invalidate_feedback(*key)
            raise ValueError("Redis MGET result length mismatch")
        now = datetime.now(UTC)
        for source, raw in zip(self._sources, records, strict=True):
            key = source.target
            try:
                value = decode_record(raw, source, now)
                await self._store.update_feedback(*key, value)
                self._healthy[key] = True
            except (ValueError, KeyError, PermissionError, TypeError):
                self._healthy[key] = False
                await self._store.invalidate_feedback(*key)
