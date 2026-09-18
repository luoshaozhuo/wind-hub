"""Unit tests for CLI output formatting helpers."""

from __future__ import annotations

import json

from wind_hub.adapter.inbound.cli.output import print_json, print_kv, print_table


def test_print_json_emits_valid_indented_json(capsys) -> None:  # noqa: ANN001
    print_json({"a": 1, "nested": {"b": True}})
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == {"a": 1, "nested": {"b": True}}
    assert "\n  " in out  # indented


def test_print_json_serializes_datetime(capsys) -> None:  # noqa: ANN001
    from datetime import datetime

    print_json({"ts": datetime(2026, 1, 2, 3, 4, 5)})
    out = capsys.readouterr().out
    assert '"ts"' in out
    # datetime is serialized as a string (via default=str), not an error
    assert "2026" in out


def test_print_table_aligns_columns(capsys) -> None:  # noqa: ANN001
    print_table(
        [
            {"device_id": "a", "protocol": "modbus", "connected": True},
            {"device_id": "longer-name", "protocol": "ads", "connected": False},
        ],
        ["device_id", "protocol", "connected"],
    )
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 4  # header + separator + 2 rows
    assert lines[0].startswith("device_id")
    assert "true" in lines[2]
    assert "false" in lines[3]


def test_print_table_missing_key_renders_empty(capsys) -> None:  # noqa: ANN001
    print_table([{"a": 1}], ["a", "b"])
    lines = capsys.readouterr().out.strip().splitlines()
    # header + separator + 1 row
    assert len(lines) == 3


def test_print_kv_aligns_values(capsys) -> None:  # noqa: ANN001
    print_kv({"device_id": "d1", "value": 3.14, "connected": True})
    out = capsys.readouterr().out
    assert "device_id" in out
    assert "d1" in out
    assert "3.14" in out
    assert "true" in out


def test_print_error_goes_to_stderr(capsys) -> None:  # noqa: ANN001
    from wind_hub.adapter.inbound.cli.output import print_error

    print_error("boom")
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert captured.out == ""
