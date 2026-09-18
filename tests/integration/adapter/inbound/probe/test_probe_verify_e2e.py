"""Integration tests for ``wind-hub probe verify``（CLI 端到端）。

协议驱动经 monkeypatch 替换（不触网），用 CliRunner 驱动完整链路：
配置加载 → verify_all 编排 → 文本/JSON/YAML 输出 → exit code 语义
（决策 7）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.probe import verify as verify_mod
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointValue, Quality


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_config(base: Path) -> None:
    """两台设备（modbus / iec104），共 3 个点。"""
    (base / "system.yaml").write_text("{}\n", encoding="utf-8")
    (base / "devices.yaml").write_text(
        yaml.safe_dump(
            {
                "devices": [
                    {
                        "device_id": "wtg-001",
                        "protocol": "modbus",
                        "point_table": "modbus-table",
                        "endpoint": {"host": "10.0.1.1", "port": 502},
                    },
                    {
                        "device_id": "wtg-002",
                        "protocol": "iec104",
                        "point_table": "iec104-table",
                        "endpoint": {"host": "10.0.1.2", "port": 2404},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (base / "points.yaml").write_text(
        yaml.safe_dump(
            {
                "point_tables": {
                    "modbus-table": {
                        "points": [
                            {
                                "point_id": "rotor.speed",
                                "address": {"type": "holding_register", "address": 0},
                                "data_type": "int16",
                            },
                            {
                                "point_id": "gen.power",
                                "address": {"type": "holding_register", "address": 1},
                                "data_type": "int16",
                            },
                        ]
                    },
                    "iec104-table": {
                        "points": [
                            {
                                "point_id": "nacelle.temp",
                                "address": {"type": "measured_value", "ioa": 1001},
                            },
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (base / "routing.yaml").write_text("rules: []\n", encoding="utf-8")


class _FakeDriver:
    """按设备行为脚本化的假驱动（values / 异常 / BAD 质量）。"""

    def __init__(
        self,
        values: list[PointValue] | None = None,
        connect_exc: Exception | None = None,
    ) -> None:
        self._values = values
        self._connect_exc = connect_exc

    def set_points_mapping(self, points: list[Any]) -> None:
        pass

    async def connect(self) -> None:
        if self._connect_exc is not None:
            raise self._connect_exc

    async def read(self, points: list[Any]) -> list[PointValue]:
        assert self._values is not None
        return self._values

    async def close(self) -> None:
        pass


def _patch_drivers(monkeypatch: pytest.MonkeyPatch, drivers: dict[str, _FakeDriver]) -> None:
    monkeypatch.setattr(
        verify_mod, "_create_driver", lambda device_cfg: drivers[device_cfg.device_id]
    )


def _good_values(device_id: str, *point_ids: str) -> list[PointValue]:
    return [
        PointValue(device_id=device_id, point_id=pid, value=1.0, quality=Quality.GOOD)
        for pid in point_ids
    ]


# ---------------------------------------------------------------------------
# 场景：全通 / 部分失败 / 质量 BAD
# ---------------------------------------------------------------------------


def test_verify_all_ok_exit_zero(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config(tmp_path)
    _patch_drivers(
        monkeypatch,
        {
            "wtg-001": _FakeDriver(values=_good_values("wtg-001", "rotor.speed", "gen.power")),
            "wtg-002": _FakeDriver(values=_good_values("wtg-002", "nacelle.temp")),
        },
    )
    result = runner.invoke(build_cli(), ["probe", "verify", "--config", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "点表只读验证" in result.output
    assert "设备: wtg-001 (modbus)" in result.output
    assert "设备: wtg-002 (iec104)" in result.output
    assert "结论: ✅ 所有点可读，质量全 GOOD" in result.output


def test_verify_partial_failure_exit_one(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """wtg-002 连接失败 → 该设备点全 fail（决策 10），exit 1。"""
    _write_config(tmp_path)
    _patch_drivers(
        monkeypatch,
        {
            "wtg-001": _FakeDriver(values=_good_values("wtg-001", "rotor.speed", "gen.power")),
            "wtg-002": _FakeDriver(connect_exc=ProtocolError("connection refused")),
        },
    )
    result = runner.invoke(build_cli(), ["probe", "verify", "--config", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "连接:        ❌ FAIL" in result.output
    assert "❌ nacelle.temp" in result.output
    assert "结论: ❌ 有失败点" in result.output


def test_verify_bad_quality_exit_two(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """质量 BAD 单列一节（决策 5），exit 2。"""
    _write_config(tmp_path)
    bad = PointValue(device_id="wtg-002", point_id="nacelle.temp", value=0.0, quality=Quality.BAD)
    _patch_drivers(
        monkeypatch,
        {
            "wtg-001": _FakeDriver(values=_good_values("wtg-001", "rotor.speed", "gen.power")),
            "wtg-002": _FakeDriver(values=[bad]),
        },
    )
    result = runner.invoke(build_cli(), ["probe", "verify", "--config", str(tmp_path)])

    assert result.exit_code == 2, result.output
    assert "质量 BAD 点:" in result.output
    assert "⚠️ nacelle.temp" in result.output
    assert "结论: ⚠️ 全部可读" in result.output


def test_verify_specific_device(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config(tmp_path)
    _patch_drivers(
        monkeypatch,
        {"wtg-001": _FakeDriver(values=_good_values("wtg-001", "rotor.speed", "gen.power"))},
    )
    result = runner.invoke(
        build_cli(),
        ["probe", "verify", "--config", str(tmp_path), "--device", "wtg-001"],
    )

    assert result.exit_code == 0, result.output
    assert "设备: wtg-001" in result.output
    assert "设备: wtg-002" not in result.output


def test_verify_unknown_device_exit_one(runner: CliRunner, tmp_path: Path) -> None:
    _write_config(tmp_path)
    result = runner.invoke(
        build_cli(),
        ["probe", "verify", "--config", str(tmp_path), "--device", "no-such"],
    )
    assert result.exit_code == 1
    assert "不存在" in result.output


# ---------------------------------------------------------------------------
# 结构化输出与参数校验
# ---------------------------------------------------------------------------


def test_verify_json_output(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config(tmp_path)
    _patch_drivers(
        monkeypatch,
        {
            "wtg-001": _FakeDriver(values=_good_values("wtg-001", "rotor.speed", "gen.power")),
            "wtg-002": _FakeDriver(values=_good_values("wtg-002", "nacelle.temp")),
        },
    )
    result = runner.invoke(build_cli(), ["probe", "verify", "--config", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["total_devices"] == 2
    assert payload["summary"]["total_points"] == 3
    assert payload["summary"]["total_ok"] == 3
    assert payload["summary"]["total_fail"] == 0
    assert payload["devices"][0]["device_id"] == "wtg-001"
    assert payload["devices"][0]["points"][0]["ok"] is True


def test_verify_yaml_output(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config(tmp_path)
    _patch_drivers(
        monkeypatch,
        {
            "wtg-001": _FakeDriver(values=_good_values("wtg-001", "rotor.speed", "gen.power")),
            "wtg-002": _FakeDriver(values=_good_values("wtg-002", "nacelle.temp")),
        },
    )
    result = runner.invoke(build_cli(), ["probe", "verify", "--config", str(tmp_path), "--yaml"])

    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.output)
    assert payload["summary"]["total_points"] == 3


def test_verify_json_yaml_conflict(runner: CliRunner, tmp_path: Path) -> None:
    _write_config(tmp_path)
    result = runner.invoke(
        build_cli(), ["probe", "verify", "--config", str(tmp_path), "--json", "--yaml"]
    )
    assert result.exit_code == 1
    assert "二选一" in result.output


def test_probe_verify_help(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "verify", "--help"])
    assert result.exit_code == 0
    for opt in [
        "--device",
        "--config",
        "--connect-timeout",
        "--read-timeout",
        "--concurrency",
        "--json",
        "--yaml",
    ]:
        assert opt in result.output
