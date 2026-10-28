"""双时间戳契约：设备时刻和接收时刻不能互相覆盖。"""

from datetime import UTC, datetime, timedelta

import pytest

from core.application.protocol_contract import ProtocolSample


def test_device_timestamp_is_preserved_separately() -> None:
    device = datetime(2026, 1, 1, tzinfo=UTC)
    received = device + timedelta(seconds=2)
    sample = ProtocolSample(
        point_id="p",
        value=1.0,
        timestamp=device,
        timestamp_source="device",
        receive_time=received,
    )
    assert sample.device_time == device
    assert sample.receive_time == received


def test_local_timestamp_has_no_device_time() -> None:
    now = datetime.now(UTC)
    sample = ProtocolSample(point_id="p", value=1, timestamp=now)
    assert sample.device_time is None
    assert sample.receive_time.tzinfo is not None


def test_rejects_naive_receive_or_device_time() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="receive_time"):
        ProtocolSample(
            point_id="p", value=1, timestamp=now, receive_time=datetime(2026, 1, 1)
        )
    with pytest.raises(ValueError, match="device_time"):
        ProtocolSample(
            point_id="p", value=1, timestamp=now, device_time=datetime(2026, 1, 1)
        )
