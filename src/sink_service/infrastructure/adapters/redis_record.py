"""Redis Sink 的稳定键和值序列化规则；不执行网络 I/O。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.domain.point_value import PointValue


class RedisKeyOptions(Protocol):
    key_prefix: str


def _format_utc(value: datetime | None) -> str | None:
    """统一 ISO-8601 UTC 字符串（Z 后缀）；None 表示该时间戳缺省。"""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RedisRecord:
    """Redis SET 的完整写入参数。"""

    key: str
    payload: str


def encode_redis_record(
    config: RedisKeyOptions,
    point: PointValue,
    *,
    key_prefix: str | None = None,
    key_override: str | None = None,
) -> RedisRecord:
    """一设备一点一个 key，覆盖最新值，保留采样时间和质量。

    为防止设备/点名称中的冒号导致键路径歧义，使用长度前缀编码。
    过期处理在 RedisSink 的原子写入过程中完成。
    """
    device = point.device_id
    name = point.point_id
    prefix = key_prefix if key_prefix is not None else config.key_prefix
    key = key_override or f"{prefix}:{len(device)}:{device}:{len(name)}:{name}"
    payload = json.dumps(
        {
            "value": point.value,
            "sample_id": point.sample_id,
            "quality": point.quality.value,
            "timestamp": _format_utc(point.timestamp),
            "source": point.source,
            "receive_time": _format_utc(point.receive_time),
            "device_time": _format_utc(point.device_time),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return RedisRecord(key=key, payload=payload)
