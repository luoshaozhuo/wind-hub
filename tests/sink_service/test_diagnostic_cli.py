"""Sink Service 诊断 CLI（wind-hub-sink-diagnostic）真实命令调用测试。

Sink 诊断在 Sink Service 独立服务化后从 core 诊断 CLI 迁出，入口为
``sink_service.diagnostic_cli:app``（命令 check / benchmark）。本测试以
CliRunner 真实驱动 Typer，仅以桩替换 check_sink / benchmark_sink 实现：

- PASS/FAIL/UNKNOWN/SKIPPED/WARNING → 退出码 0/1/3（WARNING 同 PASS）；
- --json 输出可解析、ensure_ascii=False 承载特殊字符；
- check/benchmark 子命令注册并接到实现函数。
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus
from sink_service import diagnostic_cli as sink_cli_mod
from sink_service.diagnostic_cli import app

runner = CliRunner()


def _result(status: DiagnosticStatus, *, code: str = "X", message: str = "msg") -> DiagnosticResult:
    return DiagnosticResult(
        category="test",
        target="target",
        status=status,
        findings=(DiagnosticFinding(code=code, status=status, message=message),),
        measurements={"k": 1},
        evidence={"e": True},
    )


def test_help_and_subcommand_registration() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DiagnosticStatus.FAIL, 1),
        (DiagnosticStatus.UNKNOWN, 3),
        (DiagnosticStatus.SKIPPED, 3),
        (DiagnosticStatus.WARNING, 0),
        (DiagnosticStatus.PASS, 0),
    ],
)
def test_exit_codes_by_status(
    monkeypatch: pytest.MonkeyPatch, status: DiagnosticStatus, code: int
) -> None:
    async def fake_check(*args, **kwargs) -> DiagnosticResult:
        return _result(status)

    monkeypatch.setattr(sink_cli_mod, "check_sink", fake_check)
    result = runner.invoke(app, ["check", "--sink", "s1", "--config", "x"])
    assert result.exit_code == code


def test_json_output_serializes_special_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = '中文与 emoji 🔥 以及引号 "q"'

    async def fake_check(*args, **kwargs) -> DiagnosticResult:
        return _result(DiagnosticStatus.PASS, message=message)

    monkeypatch.setattr(sink_cli_mod, "check_sink", fake_check)
    result = runner.invoke(app, ["check", "--sink", "s1", "--config", "x", "--json"])

    assert result.exit_code == 0
    text = result.stdout if hasattr(result, "stdout") else result.output
    payload = json.loads(text)
    assert payload["findings"][0]["message"] == message
    assert "🔥" in text  # ensure_ascii=False，不转义成 XXXX


def test_check_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_check(config, sink_id, *, timeout) -> DiagnosticResult:
        captured.update(config=str(config), sink_id=sink_id, timeout=timeout)
        return _result(DiagnosticStatus.PASS, code="WIRED")

    monkeypatch.setattr(sink_cli_mod, "check_sink", fake_check)
    result = runner.invoke(
        app,
        ["check", "--sink", "s1", "--config", "cfg-dir", "--timeout", "2.5"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"config": "cfg-dir", "sink_id": "s1", "timeout": 2.5}


def test_benchmark_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_benchmark(config, sink_id, *, samples, interval, timeout) -> DiagnosticResult:
        captured.update(
            config=str(config),
            sink_id=sink_id,
            samples=samples,
            interval=interval,
            timeout=timeout,
        )
        return _result(DiagnosticStatus.PASS, code="WIRED")

    monkeypatch.setattr(sink_cli_mod, "benchmark_sink", fake_benchmark)
    result = runner.invoke(
        app,
        [
            "benchmark",
            "--sink",
            "s1",
            "--config",
            "x",
            "--samples",
            "2",
            "--interval",
            "0.1",
            "--timeout",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "config": "x",
        "sink_id": "s1",
        "samples": 2,
        "interval": 0.1,
        "timeout": 3.0,
    }
