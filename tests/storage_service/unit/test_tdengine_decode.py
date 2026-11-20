"""TDengine 历史行的严格类型与时间解码。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from storage_service.domain.sample import DataLevel
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineBackend,
    TDengineSchemaConflictError,
)
from storage_service.infrastructure.adapters.outbound.tdengine_schema import expected_columns

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


class _DescribeConnection:
    def __init__(self, *, sample_key_length: int = 64) -> None:
        self.sample_key_length = sample_key_length

    def query(self, sql: str) -> list[tuple[object, ...]]:
        if sql.startswith("DESCRIBE"):
            rows: list[tuple[object, ...]] = []
            for name, declaration in expected_columns():
                base = declaration.split("(")[0].split(" ")[0]
                length = 8
                if "(" in declaration:
                    length = int(declaration.split("(")[1].split(")")[0])
                if name == "sample_key":
                    length = self.sample_key_length
                # TDengine 3.3.6.13 DESCRIBE: field, type, length, note, ...
                rows.append((name, base, length, "", "disabled", "zstd", "medium"))
            return rows
        assert sql.startswith("SHOW TAGS")
        return [
            (None, None, None, "identity_hash", None, "a" * 64),
            (None, None, None, "point_table_hash", None, "b" * 64),
            (None, None, None, "device_hash", None, "c" * 64),
            (None, None, None, "level_tag", None, "1"),
        ]


def _verify_describe(sample_key_length: int) -> None:
    names = SimpleNamespace(
        database="wh_" + "d" * 24,
        table="tb_" + "e" * 24,
        table_identity_sha256="a" * 64,
    )
    sample = SimpleNamespace(data_level=DataLevel.RAW)
    TDengineBackend._verify_table(
        cast(Any, _DescribeConnection(sample_key_length=sample_key_length)),
        cast(Any, names),
        cast(Any, sample),
        expected_point_hash="b" * 64,
        expected_device_hash="c" * 64,
    )


def test_verify_table_reads_tdengine_33613_declared_lengths() -> None:
    _verify_describe(64)
    with pytest.raises(TDengineSchemaConflictError, match="column length: sample_key"):
        _verify_describe(63)
