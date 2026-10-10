"""Redis Sink 的稳定键和值序列化规则；不执行网络 I/O。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC

from core.application.sink_config import RedisSinkConnection
from core.domain.point_value import PointValue


@dataclass(frozen=True, slots=True)
class RedisRecord:
    """Redis SET 的完整写入参数。"""

    key: str
    payload: str


def encode_redis_record(config: RedisSinkConnection, point: PointValue) -> RedisRecord:
    """一设备一点一个 key，覆盖最新值，保留采样时间和质量。

    为防止设备/点名称中的冒号导致键路径歧义，使用长度前缀编码。
    不设置过期时间；数据是否过期由消费端根据 timestamp 判定。
    """
    device = point.device_id
    name = point.point_id
    key = f"{config.key_prefix}:{len(device)}:{device}:{len(name)}:{name}"
    payload = json.dumps(
        {
            "value": point.value,
            "quality": point.quality.value,
            "timestamp": point.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "source": point.source,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return RedisRecord(key=key, payload=payload)
