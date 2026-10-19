"""双时间戳从工程点值到 Sink 记录的传递。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from core.application.redis_sink_record import encode_redis_record
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import RedisSinkConnection


def test_device_time_survives_redis_serialization() -> None:
    device = datetime(2026, 10, 1, tzinfo=UTC)
    arrival = device + timedelta(milliseconds=50)
    value = PointValue(
        device_id="WT1",
        point_id="power",
        value=1.5,
        timestamp=device,
        timestamp_source="device",
        receive_time=arrival,
        device_time=device,
    )
    data = json.loads(encode_redis_record(RedisSinkConnection(), value).payload)
    assert data["receive_time"] == "2026-10-01T00:00:00.050000Z"
    assert data["device_time"] == "2026-10-01T00:00:00Z"


def test_absent_device_time_stays_null() -> None:
    value = PointValue(device_id="WT1", point_id="p", value=1)
    data = json.loads(encode_redis_record(RedisSinkConnection(), value).payload)
    assert data["device_time"] is None


@pytest.mark.parametrize("field", ["receive_time", "device_time"])
def test_rejects_naive_times(field: str) -> None:
    kwargs = {field: datetime(2026, 10, 1)}
    with pytest.raises(ValueError, match=field):
        PointValue(device_id="WT1", point_id="p", value=1, **kwargs)
