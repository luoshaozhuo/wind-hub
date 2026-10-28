"""Redis 键组织规则测试。"""

import json
from datetime import UTC, datetime

from core.infrastructure.sink.redis_record import encode_redis_record
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import RedisSinkConnection


def test_redis_record_preserves_quality_and_time() -> None:
    point = PointValue(
        device_id="WT001",
        point_id="power",
        value=125.4,
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
        source="modbus",
    )
    record = encode_redis_record(RedisSinkConnection(key_prefix="wind"), point)
    assert record.key == "wind:5:WT001:5:power"
    payload = json.loads(record.payload)
    assert payload == {
        "value": 125.4,
        "quality": "good",
        "timestamp": "2026-10-10T00:00:00Z",
        "source": "modbus",
        "receive_time": "2026-10-10T00:00:00Z",
        "device_time": None,
    }


def test_redis_key_handles_embedded_colons() -> None:
    point = PointValue(device_id="WT:1", point_id="p:1", value=True)
    result = encode_redis_record(RedisSinkConnection(), point)
    assert result.key == "wind-hub:4:WT:1:3:p:1"


def test_redis_record_rejects_non_finite_floats() -> None:
    """strict JSON（allow_nan=False）：NaN/Inf 必须在编码层显式失败。"""
    import pytest

    for value in (float("nan"), float("inf"), float("-inf")):
        point = PointValue(device_id="WT001", point_id="p", value=value)
        with pytest.raises(ValueError):
            encode_redis_record(RedisSinkConnection(), point)
