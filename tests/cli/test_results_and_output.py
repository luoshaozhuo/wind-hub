"""ResultStore、原子导出与统一输出信封/退出码契约。"""

from __future__ import annotations

import json

import pytest

from cli.presentation import emit, response
from cli.results import ResultStore


def test_result_store_empty_and_record() -> None:
    store = ResultStore()
    empty = store.show()
    assert empty["status"] == "UNKNOWN"
    assert empty["code"] == "NO_DIAGNOSIS_RESULT"
    assert store.last is None
    store.record({"status": "PASS"})
    assert store.show()["status"] == "PASS"


def test_export_requires_result_and_json_suffix(tmp_path) -> None:
    store = ResultStore()
    with pytest.raises(ValueError, match="no automatic diagnosis"):
        store.export(tmp_path / "a.json")
    store.record({"status": "PASS"})
    with pytest.raises(ValueError, match="\\.json"):
        store.export(tmp_path / "a.txt")
    with pytest.raises(ValueError, match="directory"):
        store.export(tmp_path / "missing" / "a.json")


def test_export_atomic_json_and_no_temp_residue(tmp_path) -> None:
    store = ResultStore()
    store.record({"status": "PASS", "fingerprint": "fp-1", "steps": []})
    target = tmp_path / "report.json"
    result = store.export(target)
    assert result["status"] == "PASS"
    assert result["diagnosis_status"] == "PASS"
    payload = json.loads(target.read_text())
    assert payload["schema_version"] == 1
    assert payload["diagnosis"]["fingerprint"] == "fp-1"
    assert not list(tmp_path.glob(".*.tmp"))
    # 覆盖导出也是原子替换。
    store.export(target)
    assert json.loads(target.read_text())["diagnosis"]["fingerprint"] == "fp-1"


def test_response_envelope_contract() -> None:
    payload = {
        "status": "FAIL",
        "code": "X",
        "device_id": "d1",
        "protocol_options": {"a": 1},
        "findings": [1],
    }
    result = response("diagnose protocol check", payload, fingerprint="fp", duration_ms=1.5)
    assert result["schema_version"] == 1
    assert result["status"] == "FAIL"
    assert result["command"] == "diagnose protocol check"
    assert result["config_fingerprint"] == "fp"
    assert result["target"] == "d1"
    assert result["parameters"] == {"a": 1}
    assert result["findings"] == [1]
    assert result["duration_ms"] >= 0


def test_response_defaults_for_plain_data() -> None:
    result = response("devices", [{"x": 1}], fingerprint=None, duration_ms=0)
    assert result["status"] == "PASS"
    assert result["code"] == "OK"
    assert result["target"] is None


def test_json_emit_is_valid_json(capsys) -> None:
    result = response("config read", {"status": "PASS"}, fingerprint=None, duration_ms=0)
    emit(result, format_name="json")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "PASS"


def test_table_emit_empty_list(capsys) -> None:
    result = response("sinks", [], fingerprint=None, duration_ms=0)
    emit(result)
    assert "(empty)" in capsys.readouterr().out


def test_unknown_exit_code_three_and_fail_exit_one(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    # 无结果查询 → UNKNOWN → 退出码 3。
    rc, _, _ = run_cli(["--config", str(base), "--format", "json", "diagnose", "result"])
    assert rc == 3
    # 未加载配置的对象查询 → FAIL → 非 validate 命令退出码 1（经 default 错误信封）。
    rc, _, _ = run_cli(["devices"])
    assert rc == 2  # main 顶层捕获 ConfigError 统一 2


def test_cli_invalid_timeout_exit_two(run_cli) -> None:
    rc, _, err = run_cli(["--timeout", "0", "--help"])
    assert rc == 2
    assert "timeout" in err


def test_cli_invalid_format_exit_two(run_cli) -> None:
    rc, _, err = run_cli(["--format", "yaml"])
    assert rc == 2
    assert "format" in err


def test_cli_help_exit_zero(run_cli) -> None:
    rc, out, _ = run_cli(["--help"])
    assert rc == 0
    assert "Wind Hub CLI" in out


def test_global_flags_accepted_in_any_prefix_order(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    # 三种全局参数任意前置次序都必须被接受。
    for argv in (
        ["--format", "json", "--config", str(base), "devices"],
        ["--timeout", "5", "--config", str(base), "devices"],
        ["--format", "json", "--timeout", "5", "--config", str(base), "devices"],
        ["--config", str(base), "--format", "json", "devices"],
    ):
        rc, out, err = run_cli(argv)
        assert rc == 0, f"{argv}: {err}"
        if "--format" in argv:
            assert '"command": "devices"' in out
        else:
            assert "PASS devices" in out


def test_duplicate_global_option_rejected(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    rc, _, err = run_cli(["--config", str(base), "--config", str(base), "devices"])
    assert rc == 2
    assert "duplicate" in err
    rc, _, err = run_cli(["--format", "json", "--format", "table", "devices"])
    assert rc == 2
    assert "duplicate" in err


def test_global_option_missing_value_rejected(run_cli) -> None:
    rc, _, err = run_cli(["--config"])
    assert rc == 2
    assert "requires a value" in err
    rc, _, err = run_cli(["--format", "json", "--config"])
    assert rc == 2
    assert "requires a value" in err


def test_unknown_global_flag_rejected(run_cli) -> None:
    rc, _, err = run_cli(["--bogus", "x", "devices"])
    assert rc == 2
    assert "unknown command" in err
