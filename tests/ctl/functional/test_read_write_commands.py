"""ctl read / write 命令 functional 测试。

write 链路：CLI → gRPC → CommandDispatcher → Modbus 驱动 → fixture
server；断言写入到达协议对端（fixture server 独立客户端回读），
而非仅命令返回成功。
"""

from __future__ import annotations


import pytest

pytestmark = pytest.mark.modbus

from tests.ctl.functional.conftest import CtlEnvironment, decode_float32


class TestWriteCommand:
    def test_write_float_reaches_device_registers(
        self, run_ctl, ctl_env: CtlEnvironment
    ) -> None:
        result = run_ctl("write", "modbus-1", "setpoint.power", "2.5")
        assert result.returncode == 0, result.stderr
        assert result.payload["success"] is True

        # 独立客户端从 fixture server 侧回读——确认写入到达对端。
        registers = ctl_env.run(ctl_env.server.read_holding(1, 200, 2))
        assert decode_float32(registers) == pytest.approx(2.5)

    def test_write_int_scalar(self, run_ctl, ctl_env: CtlEnvironment) -> None:
        result = run_ctl("write", "modbus-1", "setpoint.power", "7")
        assert result.returncode == 0, result.stderr
        assert result.payload["success"] is True
        registers = ctl_env.run(ctl_env.server.read_holding(1, 200, 2))
        assert decode_float32(registers) == pytest.approx(7.0)

    def test_write_then_read_roundtrip_via_cli(self, run_ctl) -> None:
        write = run_ctl("write", "modbus-1", "setpoint.power", "3.25")
        assert write.returncode == 0, write.stderr
        read = run_ctl("read", "modbus-1", "setpoint.power")
        assert read.returncode == 0, read.stderr
        assert read.payload["value"] == pytest.approx(3.25)

    def test_write_unknown_device_returns_failed_result(self, run_ctl) -> None:
        # 指令失败内联为 CommandResult（RPC 本身成功）——退出码 0，
        # 失败事实在载荷中。
        result = run_ctl("write", "ghost", "setpoint.power", "1.0")
        assert result.returncode == 0, result.stderr
        assert result.payload["success"] is False
        assert "ghost" in result.payload["error"]

    def test_write_unknown_point_returns_failed_result(self, run_ctl) -> None:
        result = run_ctl("write", "modbus-1", "ghost.point", "1.0")
        assert result.returncode == 0, result.stderr
        assert result.payload["success"] is False
        assert "ghost.point" in result.payload["error"]


class TestReadCommand:
    def test_read_int16_point(self, run_ctl) -> None:
        result = run_ctl("read", "modbus-1", "temp.int")
        assert result.returncode == 0, result.stderr
        assert result.payload["value"] == 25
