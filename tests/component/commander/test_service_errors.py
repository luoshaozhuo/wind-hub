"""Commander 应用服务错误路径组件测试。

在真实 CommanderApp（未连接设备）上验证命令/读取/诊断的错误收敛语义：
未知设备、未知点、空点列表、设备未连接——全部收敛为 CommandError 或
``CommandResult(success=False)``，不产生未处理异常。
"""

from __future__ import annotations

import pytest

from wind_hub_commander.assembly import CommanderApp
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import CommandError


def _command(command_id: str = "cmd-1", device_id: str = "modbus-1") -> Command:
    return Command(
        command_id=command_id,
        device_id=device_id,
        point_id="setpoint.power",
        value=1.0,
        timeout=1.0,
    )


class TestCommanderCommandService:
    async def test_unknown_device_returns_failed_result(
        self, commander_app: CommanderApp
    ) -> None:
        result = await commander_app.command.send(_command(device_id="ghost"))
        assert not result.success
        assert "ghost" in (result.error or "")

    async def test_disconnected_device_returns_failed_result(
        self, commander_app: CommanderApp
    ) -> None:
        """目标端口无服务——连接失败必须收敛为结果，不向上抛。"""
        result = await commander_app.command.send(_command("cmd-disc"))
        assert not result.success
        assert "not connected" in (result.error or "")

    async def test_send_batch_preserves_order_and_isolates_failures(
        self, commander_app: CommanderApp
    ) -> None:
        results = await commander_app.command.send_batch(
            [
                _command("cmd-a", device_id="ghost"),
                _command("cmd-b"),
            ]
        )
        assert [r.command_id for r in results] == ["cmd-a", "cmd-b"]
        assert all(not r.success for r in results)
        assert "ghost" in (results[0].error or "")


class TestCommanderReadService:
    async def test_unknown_device_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="unknown device"):
            await commander_app.read.read_point("ghost", "rotor.speed")

    async def test_empty_point_list_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="non-empty"):
            await commander_app.read.read_points("modbus-1", [])

    async def test_unknown_point_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="unknown points"):
            await commander_app.read.read_point("modbus-1", "no.such.point")

    async def test_disconnected_device_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="not connected"):
            await commander_app.read.read_point("modbus-1", "rotor.speed")


class TestCommanderDiagnosticService:
    async def test_verify_unknown_device_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="unknown device"):
            await commander_app.diagnostic.verify_device("ghost")

    async def test_resolve_unknown_point_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="unknown point"):
            await commander_app.diagnostic.resolve_point("modbus-1", "no.such.point")

    async def test_resolve_point_returns_configured_address(
        self, commander_app: CommanderApp
    ) -> None:
        """非 ADS 协议的 resolve 不触网——直接返回配置地址事实。"""
        result = await commander_app.diagnostic.resolve_point("modbus-1", "setpoint.power")
        assert result.ok
        assert result.configured_address["register_type"] == "holding"
        assert result.configured_address["address"] == 200
        assert result.resolved_address is not None

    async def test_verify_device_unreachable_reports_stages(
        self, commander_app: CommanderApp
    ) -> None:
        """设备不可达时诊断仍返回分层结果（非异常），protocol 阶段失败。"""
        result = await commander_app.diagnostic.verify_device("modbus-1", timeout=0.3)
        assert not result.ok
        by_name = {stage.name: stage for stage in result.stages}
        assert by_name["transport"].ok is False
        assert by_name["protocol"].ok is False

    async def test_verify_points_unknown_group_raises_command_error(
        self, commander_app: CommanderApp
    ) -> None:
        with pytest.raises(CommandError, match="point group"):
            await commander_app.diagnostic.verify_points("modbus-1", point_group="ghost")
