"""TDengine WebSocket timestamps must retain microseconds and timezone semantics."""

import pytest

from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineBackend,
    TDengineSchemaConflictError,
)


def test_datetime_us_decodes_aware_websocket_timestamp() -> None:
    assert TDengineBackend._datetime_us("2027-01-15 16:00:00.123456 +08:00") == (
        1_800_000_000_123_456
    )


def test_datetime_us_decodes_decimal_websocket_timestamp() -> None:
    assert TDengineBackend._datetime_us("1800000000123456") == 1_800_000_000_123_456


def test_datetime_us_decodes_millisecond_with_spaced_timezone() -> None:
    assert TDengineBackend._datetime_us("2026-11-26 17:41:42.742 +08:00") == (1_795_686_102_742_000)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2027-01-15 16:00:00.123456",
        "not-a-timestamp",
    ],
)
def test_datetime_us_rejects_invalid_websocket_timestamp(timestamp: str) -> None:
    with pytest.raises(TDengineSchemaConflictError):
        TDengineBackend._datetime_us(timestamp)
