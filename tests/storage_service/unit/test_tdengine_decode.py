"""TDengine 历史行的严格类型与时间解码。"""

from __future__ import annotations

import pytest

from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineBackend,
    TDengineSchemaConflictError,
)

pytestmark = pytest.mark.unit


def _row() -> list[object]:
    return [
        1_000_001,
        "a" * 64,
        "b" * 64,
        "sample-1",
        "point-1",
        "business-1",
        1,
        2,
        42,
        None,
        None,
        None,
        None,
        None,
        1,
        1_000_000,
        1_000_002,
    ]


def test_decode_history_row_preserves_optional_receive_time() -> None:
    identity = ("farm", "type", "device", "table", "raw", "collector")
    decoded = TDengineBackend._decode_record(_row(), identity)
    assert decoded.value == 42
    assert decoded.receive_time_us == 1_000_002
    row = _row()
    row[16] = None
    decoded_without_receive = TDengineBackend._decode_record(row, identity)
    assert decoded_without_receive.receive_time_us is None


def test_decode_history_row_rejects_string_typed_integer() -> None:
    row = _row()
    row[7] = "2"
    with pytest.raises(TDengineSchemaConflictError, match="lost its type"):
        TDengineBackend._decode_record(row, ("farm", "type", "device", "table", "raw", "collector"))


def test_sql_filter_accepts_utf8_and_escapes_quotes() -> None:
    assert TDengineBackend._sql_filter("有功功率") == "'有功功率'"
    assert TDengineBackend._sql_filter("O'Neil") == "'O''Neil'"
    with pytest.raises(ValueError, match="control characters"):
        TDengineBackend._sql_filter("bad\x00identity")
