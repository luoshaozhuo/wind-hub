"""Redis 键组织及 CSV 段文件规则测试。"""

import json
from datetime import UTC, datetime

import pytest

from core.application.file_sink_segments import is_owned_segment, segment_filename
from core.application.redis_sink_record import encode_redis_record
from core.application.sink_contract import RedisSinkConnection
from core.domain.point_value import PointValue


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
    }


def test_redis_key_handles_embedded_colons() -> None:
    point = PointValue(device_id="WT:1", point_id="p:1", value=True)
    result = encode_redis_record(RedisSinkConnection(), point)
    assert result.key == "wind-hub:4:WT:1:3:p:1"


def test_segment_name_is_utc_and_owned() -> None:
    name = segment_filename(
        "telemetry.csv", opened_at=datetime(2026, 10, 10, tzinfo=UTC), sequence=0
    )
    assert name == "telemetry.20261010T000000000000Z.000000.csv"
    assert is_owned_segment(name, base="telemetry.csv")
    assert not is_owned_segment(name, base="unrelated.csv")
    assert not is_owned_segment("telemetry.other.csv", base="telemetry.csv")


@pytest.mark.parametrize("sequence", [-1, -10])
def test_segment_rejects_negative_sequence(sequence: int) -> None:
    with pytest.raises(ValueError):
        segment_filename(
            "telemetry.csv", opened_at=datetime(2026, 10, 10, tzinfo=UTC),
            sequence=sequence,
        )
