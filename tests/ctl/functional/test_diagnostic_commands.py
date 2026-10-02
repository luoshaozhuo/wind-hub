"""ctl 诊断命令 functional 测试——verify-device / resolve-point /
verify-point / verify-points，经 gRPC 触发真实诊断链路。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.modbus


class TestVerifyDevice:
    def test_verify_device_ok(self, run_ctl) -> None:
        result = run_ctl("verify-device", "modbus-1")
        assert result.returncode == 0, result.stderr
        assert result.payload["device_id"] == "modbus-1"
        assert result.payload["ok"] is True
        stages = {stage["name"]: stage for stage in result.payload["stages"]}
        assert stages["transport"]["ok"] is True
        assert stages["protocol"]["ok"] is True

    def test_verify_device_unknown_exits_2(self, run_ctl) -> None:
        result = run_ctl("verify-device", "ghost")
        assert result.returncode == 2
        assert "RPC failed" in result.stderr


class TestResolvePoint:
    def test_resolve_point_reports_configured_address(self, run_ctl) -> None:
        result = run_ctl("resolve-point", "modbus-1", "rotor.speed")
        assert result.returncode == 0, result.stderr
        assert result.payload["point_id"] == "rotor.speed"
        assert result.payload["configured_address"]["address"] == 100
        assert result.payload["configured_address"]["register_type"] == "holding"


class TestVerifyPoint:
    def test_verify_point_reports_raw_and_engineering(self, run_ctl) -> None:
        result = run_ctl("verify-point", "modbus-1", "gen.power")
        assert result.returncode == 0, result.stderr
        assert result.payload["ok"] is True
        assert result.payload["raw_value"] == pytest.approx(800.0)
        assert result.payload["engineering_value"] == pytest.approx(1610.0)

    def test_verify_point_unknown_exits_2(self, run_ctl) -> None:
        result = run_ctl("verify-point", "modbus-1", "ghost.point")
        assert result.returncode == 2
        assert "RPC failed" in result.stderr


class TestVerifyPoints:
    def test_verify_points_group(self, run_ctl) -> None:
        result = run_ctl("verify-points", "modbus-1", "--group", "telemetry")
        assert result.returncode == 0, result.stderr
        assert result.payload["checked"] == 4
        assert result.payload["passed"] == 4
        assert result.payload["ok"] is True

    def test_verify_points_full_table(self, run_ctl) -> None:
        result = run_ctl("verify-points", "modbus-1")
        assert result.returncode == 0, result.stderr
        assert result.payload["point_group"] is None
        assert result.payload["checked"] == 4
        assert result.payload["ok"] is True

    def test_verify_points_unknown_group_exits_2(self, run_ctl) -> None:
        result = run_ctl("verify-points", "modbus-1", "--group", "ghost")
        assert result.returncode == 2
        assert "RPC failed" in result.stderr
