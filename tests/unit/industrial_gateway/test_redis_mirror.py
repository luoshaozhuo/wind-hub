"""Redis 反馈镜像：质量、新鲜度和故障失效验证。"""

from datetime import UTC, datetime, timedelta
import json

import pytest

from industrial_gateway.domain import PointDefinition, PointRole
from industrial_gateway.point_store import PointStore
from industrial_gateway.redis_mirror import RedisMirror, RedisPointSource, decode_record


class FakeRedis:
    def __init__(self, values: list[bytes | None]) -> None:
        self.values = values
        self.keys: list[str] = []

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        self.keys = keys
        return self.values


def _payload(value: float, *, quality: str = "good", age: float = 0) -> bytes:
    return json.dumps({
        "value": value,
        "quality": quality,
        "timestamp": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        "source": "collector",
        "receive_time": None,
        "device_time": None,
    }).encode()


async def test_mget_feedback_refresh_and_stale_invalidation() -> None:
    source = RedisPointSource("t1", "power", "points", max_age_seconds=2)
    store = PointStore([PointDefinition("t1", "power", PointRole.FEEDBACK)])
    mirror = RedisMirror(store, [source])
    client = FakeRedis([_payload(123.5)])
    await mirror.refresh(client)
    assert client.keys == [source.key]
    assert mirror.healthy("t1", "power")
    assert store.read("t1", "power").value == 123.5

    client.values = [_payload(123.5, age=10)]
    await mirror.refresh(client)
    assert not mirror.healthy("t1", "power")
    assert store.read("t1", "power").value is None


def test_quality_and_missing_key_rejected() -> None:
    source = RedisPointSource("t1", "power", "points")
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        decode_record(None, source, now)
    with pytest.raises(ValueError, match="quality"):
        decode_record(_payload(1.0, quality="bad"), source, now)
