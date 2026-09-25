"""Integration tests for ``wind-hub probe discover``（CLI 端到端，mock 后端）。

用 CliRunner 驱动完整命令链路（参数解析 → 配置加载 → 协议分派 → YAML
输出），真实网络层（符号上传 / 寄存器扫描）以 monkeypatch 替换。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.config_helper import write_config_tree
from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.commands import probe_discover
from wind_hub.adapter.inbound.cli.probe.models import DiscoveredPoint


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_config(base: Path, devices: list[dict]) -> None:
    write_config_tree(base, devices=devices, point_tables={"main": {"points": []}})


def _ads_config(base: Path) -> None:
    _write_config(
        base,
        [
            {
                "device_id": "plc-001",
                "protocol": "ads",
                "point_table": "main",
                "endpoint": {
                    "host": "10.0.3.1",
                    "port": 48898,
                    "extensions": {"target_net_id": "10.0.3.1.1.1"},
                },
            }
        ],
    )


def _modbus_config(base: Path) -> None:
    _write_config(
        base,
        [
            {
                "device_id": "wtg-002",
                "protocol": "modbus",
                "point_table": "main",
                "endpoint": {"host": "10.0.2.1", "port": 502, "extensions": {"unit_id": 1}},
            }
        ],
    )


def _iec104_config(base: Path) -> None:
    _write_config(
        base,
        [
            {
                "device_id": "wtg-001",
                "protocol": "iec104",
                "point_table": "main",
                "endpoint": {"host": "10.0.1.1", "port": 2404},
            }
        ],
    )


def _fake_ads_points(
    monkeypatch: pytest.MonkeyPatch,
    points: list[DiscoveredPoint] | None = None,
) -> list[DiscoveredPoint]:
    points = (
        points
        if points is not None
        else [
            DiscoveredPoint(symbol="MAIN.风机1.转速", data_type="float32", size=4, comment="转速")
        ]
    )

    async def _fake(device_cfg, filter_prefix=None):  # noqa: ANN001, ANN202
        if filter_prefix is not None:
            return [p for p in points if p.symbol.startswith(filter_prefix)]
        return points

    monkeypatch.setattr(probe_discover.probe_discover, "discover_ads", _fake)
    return points


# ---------------------------------------------------------------------------
# ADS 路径
# ---------------------------------------------------------------------------


def test_discover_ads_to_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ads_config(tmp_path)
    _fake_ads_points(monkeypatch)

    result = runner.invoke(
        build_cli(), ["probe", "discover", "--device", "plc-001", "--config", str(tmp_path / "site")]
    )

    assert result.exit_code == 0
    assert "此文件由 wind-hub probe discover 生成" in result.output
    data = yaml.safe_load(result.output)
    # 草稿为 point_tables 结构（点表设备无关，默认表名取设备 id）
    entries = data["point_tables"]["plc-001"]["points"]
    assert entries[0]["point_id"] == "风机1.转速"
    assert "device_id" not in entries[0]
    assert entries[0]["address"] == {"symbol": "MAIN.风机1.转速"}


def test_discover_ads_to_file(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ads_config(tmp_path)
    _fake_ads_points(monkeypatch)
    out = tmp_path / "discovered.yaml"

    result = runner.invoke(
        build_cli(),
        [
            "probe",
            "discover",
            "--device",
            "plc-001",
            "--config",
            str(tmp_path / "site"),
            "--output",
            str(out),
        ],
    )

    assert result.exit_code == 0
    assert "草稿已写入" in result.output
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["point_tables"]["plc-001"]["points"][0]["data_type"] == "float32"


def test_discover_ads_filter_prefix(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ads_config(tmp_path)
    _fake_ads_points(
        monkeypatch,
        [
            DiscoveredPoint(symbol="MAIN.a", data_type="bool", size=1),
            DiscoveredPoint(symbol="GVL.b", data_type="bool", size=1),
        ],
    )

    result = runner.invoke(
        build_cli(),
        [
            "probe",
            "discover",
            "--device",
            "plc-001",
            "--config",
            str(tmp_path / "site"),
            "--filter",
            "MAIN.",
        ],
    )

    assert result.exit_code == 0
    data = yaml.safe_load(result.output)
    entries = data["point_tables"]["plc-001"]["points"]
    assert [p["point_id"] for p in entries] == ["a"]


# ---------------------------------------------------------------------------
# Modbus 路径
# ---------------------------------------------------------------------------


def test_discover_modbus_requires_unsafe(runner: CliRunner, tmp_path: Path) -> None:
    _modbus_config(tmp_path)
    result = runner.invoke(
        build_cli(), ["probe", "discover", "--device", "wtg-002", "--config", str(tmp_path / "site")]
    )
    assert result.exit_code == 1
    assert "--unsafe" in result.output


def test_discover_modbus_requires_range_with_unsafe(runner: CliRunner, tmp_path: Path) -> None:
    _modbus_config(tmp_path)
    result = runner.invoke(
        build_cli(),
        ["probe", "discover", "--device", "wtg-002", "--config", str(tmp_path / "site"), "--unsafe"],
    )
    assert result.exit_code == 1
    assert "--range" in result.output


def test_discover_modbus_scan_outputs_register_draft(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modbus_config(tmp_path)

    async def _fake_scan(device_cfg, ranges):  # noqa: ANN001, ANN202
        assert ranges == [(0, 2)]
        return [
            DiscoveredPoint(
                symbol="holding[0]",
                data_type="int16",
                size=2,
                comment="扫描结果，需人工确认",
                address={"register_type": "holding", "address": 0},
            )
        ]

    monkeypatch.setattr(probe_discover.probe_discover, "discover_modbus", _fake_scan)

    result = runner.invoke(
        build_cli(),
        [
            "probe",
            "discover",
            "--device",
            "wtg-002",
            "--config",
            str(tmp_path / "site"),
            "--unsafe",
            "--range",
            "0-2",
        ],
    )

    assert result.exit_code == 0
    assert "寄存器扫描结果" in result.output
    data = yaml.safe_load(result.output)
    assert data["point_tables"]["wtg-002"]["points"][0]["address"] == {
        "register_type": "holding",
        "address": 0,
    }


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


def test_discover_iec104_reports_unsupported(runner: CliRunner, tmp_path: Path) -> None:
    _iec104_config(tmp_path)
    result = runner.invoke(
        build_cli(), ["probe", "discover", "--device", "wtg-001", "--config", str(tmp_path / "site")]
    )
    assert result.exit_code == 1
    assert "IEC104 不支持点表发现" in result.output


def test_discover_unknown_device(runner: CliRunner, tmp_path: Path) -> None:
    _ads_config(tmp_path)
    result = runner.invoke(
        build_cli(), ["probe", "discover", "--device", "no-such", "--config", str(tmp_path / "site")]
    )
    assert result.exit_code == 1
    assert "不存在" in result.output


def test_discover_invalid_config_dir(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        build_cli(), ["probe", "discover", "--device", "plc-001", "--config", str(tmp_path / "site")]
    )
    assert result.exit_code == 1


def test_probe_discover_help(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "discover", "--help"])
    assert result.exit_code == 0
    for opt in ["--device", "--config", "--output", "--filter", "--unsafe", "--range"]:
        assert opt in result.output
