"""Integration tests for ``wind-hub probe diagnose``（CLI 端到端）。

回环用例用真实本机信息 + 真实 ARP/ICMP/TCP 层（只碰 127.0.0.1 与
RFC 5737 保留地址 192.0.2.1），协议层按规范 mock；exit code 语义
（决策 7）用假诊断结果直接验证。
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.config_helper import write_config_tree
from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.commands import probe_diagnose
from wind_hub.adapter.inbound.cli.probe import diagnose as diag
from wind_hub.adapter.inbound.cli.probe.diagnose_models import (
    DiagnoseResult,
    DiagnoseStep,
    LocalInfo,
    StepStatus,
)
from wind_hub.domain.model.point import PointValue


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def listening_socket() -> Iterator[socket.socket]:
    """回环随机端口的真实监听 socket（供传输层真实连通）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    yield srv
    srv.close()


def _write_config(
    base: Path,
    *,
    device_id: str = "wtg-001",
    protocol: str = "modbus",
    host: str = "127.0.0.1",
    port: int = 502,
    with_point: bool = True,
) -> None:
    points: list[dict[str, Any]] = []
    if with_point:
        points.append(
            {
                "point_id": "rotor.speed",
                "point_groups": ["telemetry"],
                "address": {"type": "holding_register", "address": 0},
                "data_type": "int16",
            }
        )
    write_config_tree(
        base,
        devices=[
            {
                "device_id": device_id,
                "protocol": protocol,
                "point_table": "wtg-table",
                "endpoint": {"host": host, "port": port, "extensions": {"unit_id": 1}},
            }
        ],
        point_tables={"wtg-table": {"points": points}},
    )


class _FakeDriver:
    """协议层假驱动（e2e：只验证诊断链路，不碰真实协议栈）。"""

    def set_points_mapping(self, points: list[Any]) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def read(self, points: list[Any]) -> list[Any]:
        return [PointValue(device_id="wtg-001", point_id=points[0].point_id, value=1.0)]

    async def close(self) -> None:
        pass


def _patch_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag, "_create_driver", lambda device_cfg: _FakeDriver())


# ---------------------------------------------------------------------------
# 真实分层（回环 / 保留地址），协议层 mock
# ---------------------------------------------------------------------------


def test_diagnose_loopback_all_ok(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listening_socket: socket.socket,
) -> None:
    """诊断回环设备：网络层（回环免 ARP + ping）→ 传输层（真实监听端口）
    → 协议层（mock）应全通，exit code 0。"""
    port = listening_socket.getsockname()[1]
    _write_config(tmp_path, host="127.0.0.1", port=port)
    _patch_driver(monkeypatch)
    result = runner.invoke(
        build_cli(),
        ["probe", "diagnose", "--device", "wtg-001", "--config", str(tmp_path / "site")],
    )
    assert result.exit_code == 0, result.output
    assert "诊断结论: ✅ OK" in result.output
    assert "本机信息" in result.output
    assert "目标设备" in result.output
    assert "网络层" in result.output
    assert "传输层" in result.output
    assert "协议层" in result.output


def test_diagnose_unreachable_ip_fails(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """诊断 RFC 5737 保留地址 192.0.2.1：网络层失败 → 下层短路，exit 1。"""
    _write_config(tmp_path, host="192.0.2.1", port=2404, protocol="iec104")
    result = runner.invoke(
        build_cli(),
        [
            "probe",
            "diagnose",
            "--device",
            "wtg-001",
            "--config",
            str(tmp_path / "site"),
            "--timeout",
            "1",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "诊断结论: ❌ FAIL" in result.output
    assert "skip" in result.output  # 传输层/协议层被短路


# ---------------------------------------------------------------------------
# exit code 语义（决策 7）：用假诊断结果分别触发 0/1/2
# ---------------------------------------------------------------------------


def _fake_result(overall: StepStatus) -> DiagnoseResult:
    return DiagnoseResult(
        local_info=LocalInfo(hostname="h", ips=[("10.0.1.100/24", "eth0")], default_gateway=None),
        device_id="wtg-001",
        device_ip="10.0.1.1",
        device_port=502,
        protocol="modbus",
        steps=[DiagnoseStep(name="ICMP", status=overall, detail="fake")],
        overall=overall,
        conclusion="fake conclusion",
        same_subnet=True,
    )


@pytest.mark.parametrize(
    ("overall", "expected_exit"),
    [(StepStatus.OK, 0), (StepStatus.FAIL, 1), (StepStatus.WARN, 2)],
)
def test_exit_code_semantics(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overall: StepStatus,
    expected_exit: int,
) -> None:
    _write_config(tmp_path)

    async def _fake(
        device_cfg: Any, points: list[Any], timeout: float, connect_timeout: float
    ) -> DiagnoseResult:
        return _fake_result(overall)

    monkeypatch.setattr(probe_diagnose, "diagnose_device", _fake)
    result = runner.invoke(
        build_cli(),
        ["probe", "diagnose", "--device", "wtg-001", "--config", str(tmp_path / "site")],
    )
    assert result.exit_code == expected_exit, result.output


def test_json_output(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(tmp_path)

    async def _fake(
        device_cfg: Any, points: list[Any], timeout: float, connect_timeout: float
    ) -> DiagnoseResult:
        return _fake_result(StepStatus.OK)

    monkeypatch.setattr(probe_diagnose, "diagnose_device", _fake)
    result = runner.invoke(
        build_cli(),
        ["probe", "diagnose", "--device", "wtg-001", "--config", str(tmp_path / "site"), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["device_id"] == "wtg-001"
    assert payload["overall"] == "ok"
    assert payload["local_info"]["hostname"] == "h"
    assert payload["steps"][0]["name"] == "ICMP"


def test_unknown_device_exit_one(runner: CliRunner, tmp_path: Path) -> None:
    _write_config(tmp_path)
    result = runner.invoke(
        build_cli(),
        ["probe", "diagnose", "--device", "no-such", "--config", str(tmp_path / "site")],
    )
    assert result.exit_code == 1
    assert "不存在" in result.output


def test_probe_diagnose_help(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "diagnose", "--help"])
    assert result.exit_code == 0
    for opt in ["--device", "--config", "--timeout", "--connect-timeout", "--json"]:
        assert opt in result.output
