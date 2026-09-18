"""Integration tests for ``wind-hub probe scan``（CLI 端到端，mock 后端）。

用 CliRunner 驱动完整命令链路（参数解析 → 扫描编排 → 输出格式化），
真实网络扫描以 monkeypatch 替换——不扫描真实网段（决策 12）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.commands import probe_scan
from wind_hub.adapter.inbound.cli.probe.scan_models import ScanResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _not_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定为非 WSL 环境——WSL warning 走 stderr，避免污染 JSON/YAML 断言，
    也让测试在 WSL 与原生 Linux 上行为一致（WSL 分支由专门用例覆盖）。"""
    monkeypatch.setattr(probe_scan, "is_wsl", lambda: False)


def _fake_results() -> list[ScanResult]:
    return [
        ScanResult(
            ip="10.0.1.1",
            alive=True,
            mac="aa:bb:cc:dd:ee:01",
            detected_by=["arp", "icmp"],
        ),
        ScanResult(ip="10.0.1.2", alive=True, detected_by=["icmp"]),
        ScanResult(ip="10.0.1.10", alive=True, detected_by=["tcp"]),
    ]


def _patch_scan(monkeypatch: pytest.MonkeyPatch, results: list[ScanResult]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake(network: str, **kwargs: Any) -> list[ScanResult]:
        captured["network"] = network
        captured.update(kwargs)
        return results

    monkeypatch.setattr(probe_scan, "scan_network", _fake)
    return captured


# ---------------------------------------------------------------------------
# 表格输出（默认）
# ---------------------------------------------------------------------------


def test_scan_table_output(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_scan(monkeypatch, _fake_results())
    result = runner.invoke(build_cli(), ["probe", "scan", "--network", "10.0.1.0/24"])

    assert result.exit_code == 0
    assert captured["network"] == "10.0.1.0/24"
    assert captured["use_icmp"] is True and captured["use_tcp"] is True
    assert "10.0.1.1" in result.output
    assert "aa:bb:cc:dd:ee:01" in result.output
    assert "arp,icmp" in result.output
    assert "共扫描 254 个 IP，占用 3 个（arp: 1, icmp: 2, tcp: 1）" in result.output


def test_scan_method_flags(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_scan(monkeypatch, [])
    result = runner.invoke(
        build_cli(),
        [
            "probe",
            "scan",
            "--network",
            "10.0.1.0/24",
            "--no-icmp",
            "--timeout",
            "0.5",
            "--concurrency",
            "16",
        ],
    )
    assert result.exit_code == 0
    assert captured["use_icmp"] is False
    assert captured["use_tcp"] is True
    assert captured["timeout"] == 0.5
    assert captured["concurrency"] == 16


# ---------------------------------------------------------------------------
# JSON / YAML 输出
# ---------------------------------------------------------------------------


def test_scan_json_output(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_scan(monkeypatch, _fake_results())
    result = runner.invoke(build_cli(), ["probe", "scan", "--network", "10.0.1.0/24", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["network"] == "10.0.1.0/24"
    assert payload["total_scanned"] == 254
    assert payload["alive_count"] == 3
    assert payload["results"][0] == {
        "ip": "10.0.1.1",
        "alive": True,
        "mac": "aa:bb:cc:dd:ee:01",
        "hostname": None,
        "detected_by": ["arp", "icmp"],
    }


def test_scan_yaml_output(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_scan(monkeypatch, _fake_results())
    result = runner.invoke(build_cli(), ["probe", "scan", "--network", "10.0.1.0/24", "--yaml"])
    assert result.exit_code == 0
    payload = yaml.safe_load(result.output)
    assert payload["alive_count"] == 3
    assert payload["results"][2]["detected_by"] == ["tcp"]


def test_scan_json_and_yaml_conflict(runner: CliRunner) -> None:
    result = runner.invoke(
        build_cli(), ["probe", "scan", "--network", "10.0.1.0/24", "--json", "--yaml"]
    )
    assert result.exit_code == 1
    assert "二选一" in result.output


# ---------------------------------------------------------------------------
# 错误处理与帮助
# ---------------------------------------------------------------------------


def test_scan_invalid_network_exit_one(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "scan", "--network", "not-a-network"])
    assert result.exit_code == 1
    assert "扫描失败" in result.output


def test_scan_loopback_network_parses(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """127.0.0.1/32（冒烟用网段）：解析为 1 个 IP，命令链路完整走通。"""
    _patch_scan(
        monkeypatch,
        [ScanResult(ip="127.0.0.1", alive=True, detected_by=["icmp"])],
    )
    result = runner.invoke(build_cli(), ["probe", "scan", "--network", "127.0.0.1/32"])
    assert result.exit_code == 0
    assert "共扫描 1 个 IP，占用 1 个" in result.output


def test_scan_wsl_warning_printed(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """决策 0.2：WSL 环境下向 stderr 打 warning（不阻断扫描）。"""
    monkeypatch.setattr(probe_scan, "is_wsl", lambda: True)
    errors: list[str] = []
    monkeypatch.setattr(probe_scan, "print_error", errors.append)
    _patch_scan(monkeypatch, [])

    result = runner.invoke(build_cli(), ["probe", "scan", "--network", "10.0.1.0/24"])

    assert result.exit_code == 0
    assert any("WSL2" in msg and "mirrored" in msg for msg in errors)


def test_probe_scan_help(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "scan", "--help"])
    assert result.exit_code == 0
    for opt in [
        "--network",
        "--timeout",
        "--concurrency",
        "--resolve-hostname",
        "--no-icmp",
        "--no-tcp",
        "--json",
        "--yaml",
    ]:
        assert opt in result.output
