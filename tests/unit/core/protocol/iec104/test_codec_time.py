"""IEC104 原始品质与时间戳的保守映射。"""

from __future__ import annotations

from datetime import UTC, datetime

from core.application.protocol_contract import Quality
from core.infrastructure.protocol.iec104.codec import (
    _quality_from_c104,
    _timestamp_from_c104,
)


def test_absent_quality_is_not_treated_as_good() -> None:
    assert _quality_from_c104(None) is Quality.UNCERTAIN


def test_missing_device_timestamp_gets_local_utc_time() -> None:
    timestamp, source = _timestamp_from_c104(None)
    assert source == "local"
    assert timestamp.tzinfo is UTC


def test_naive_device_timestamp_is_not_assumed_utc() -> None:
    timestamp, source = _timestamp_from_c104(datetime(2026, 1, 1))
    assert source == "local"
    assert timestamp.tzinfo is UTC


def test_aware_device_timestamp_is_preserved() -> None:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    timestamp, source = _timestamp_from_c104(stamp)
    assert timestamp == stamp
    assert source == "device"
