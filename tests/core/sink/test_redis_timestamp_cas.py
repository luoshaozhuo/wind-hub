"""真实 Redis 集成：时间戳 CAS 防旧值覆盖与非有限值丢弃。

多 Collector（或重试/乱序链路）并发写同一 key 时，迟到的旧批次不得
回滚已落库的新值。需要真实 Redis（WIND_HUB_TEST_REDIS 或 Docker
Compose 栈），不可用则 skip，绝不退化为 mock。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.redis import RedisSink
from tests.support.redis_client import RedisTestClient, parse_address, point_key

pytestmark = [pytest.mark.redis, pytest.mark.real_service]


def _point(value: float, *, timestamp: datetime, point_id: str = "power") -> PointValue:
    return PointValue("WT001", point_id, value, timestamp=timestamp)


async def test_stale_timestamp_does_not_overwrite_fresh_value(redis_service: str) -> None:
    host, port = parse_address(redis_service)
    cfg = Sink(
        sink_id="cache",
        kind="redis",
        enabled=True,
        connection={"host": host, "port": port, "key_prefix": "wind-hub-it-cas"},
    )
    key = point_key("WT001", "power", prefix="wind-hub-it-cas")
    sink = RedisSink(cfg)
    client = RedisTestClient(redis_service)
    newer = datetime.now(UTC)
    older = newer - timedelta(seconds=30)
    try:
        await sink.open()
        await client.connect()
        await client._request("DEL", key)

        # 先写新值，再补写旧值：旧值必须被服务端 Lua CAS 拒绝。
        await sink.write([_point(200.0, timestamp=newer)])
        await sink.write([_point(100.0, timestamp=older)])

        stored = json.loads((await client.get(key)) or "null")
        assert stored["value"] == 200.0

        # 同时间戳视为合法（>= 允许覆盖），乱序后的更新值可落库。
        await sink.write([_point(201.0, timestamp=newer)])
        stored = json.loads((await client.get(key)) or "null")
        assert stored["value"] == 201.0

        # 真正更新的时间戳正常覆盖。
        await sink.write([_point(300.0, timestamp=newer + timedelta(seconds=5))])
        stored = json.loads((await client.get(key)) or "null")
        assert stored["value"] == 300.0
    finally:
        await client._request("DEL", key)
        await client.close()
        await sink.close()


async def test_non_finite_values_skipped_but_batch_delivered(redis_service: str) -> None:
    host, port = parse_address(redis_service)
    cfg = Sink(
        sink_id="cache",
        kind="redis",
        enabled=True,
        connection={"host": host, "port": port, "key_prefix": "wind-hub-it-cas"},
    )
    good_key = point_key("WT001", "good", prefix="wind-hub-it-cas")
    nan_key = point_key("WT001", "bad_nan", prefix="wind-hub-it-cas")
    sink = RedisSink(cfg)
    client = RedisTestClient(redis_service)
    now = datetime.now(UTC)
    try:
        await sink.open()
        await client.connect()
        await client._request("DEL", good_key, nan_key)

        await sink.write(
            [
                _point(42.0, timestamp=now, point_id="good"),
                _point(float("nan"), timestamp=now, point_id="bad_nan"),
            ]
        )

        assert await client.get(good_key) is not None
        assert await client.get(nan_key) is None
    finally:
        await client._request("DEL", good_key, nan_key)
        await client.close()
        await sink.close()
