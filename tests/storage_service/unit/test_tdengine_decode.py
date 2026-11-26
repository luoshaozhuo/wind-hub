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
