"""按需现场诊断 functional 测试——verify-device / verify-point(s)。

诊断链路复用真实 Runtime 设备对象与真实 Modbus fixture：验证三层
链路阶段、单点 raw/engineering value、批量验证聚合与各类失败形态
（连接失败、读失败、未知设备/点/组）。
"""

from __future__ import annotations

import pytest

from tests.component.collector.conftest import FunctionalContext
from wind_hub_core.model.errors import CommandError

pytestmark = pytest.mark.modbus


def _stage(result, name: str):
    stage = next((s for s in result.stages if s.name == name), None)
    assert stage is not None, f"missing stage {name!r} in {result.stages}"
    return stage


class TestVerifyDevice:
    async def test_verify_device_all_stages_ok(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        result = await modbus_runtime.rt.diagnostic.verify_device("modbus-1")
        assert result.device_id == "modbus-1"
        assert result.protocol == "modbus"
        # ok 聚合只由 transport + protocol 决定（ICMP 可能受环境限制）。
        assert result.ok is True
        assert _stage(result, "transport").ok is True
        assert _stage(result, "protocol").ok is True

    async def test_verify_device_unreachable_endpoint(
        self, runtime_factory
    ) -> None:
        async with runtime_factory(with_server=False) as ctx:
            result = await ctx.rt.diagnostic.verify_device("modbus-1")
            assert result.ok is False
            assert _stage(result, "transport").ok is False
            protocol = _stage(result, "protocol")
            assert protocol.ok is False
            # TCP 不可达时协议阶段必须显式跳过并说明原因。
            assert "skipped" in protocol.message

    async def test_verify_device_unknown_device(self, runtime_factory) -> None:
        async with runtime_factory() as ctx:
            with pytest.raises(CommandError, match="unknown device"):
                await ctx.rt.diagnostic.verify_device("ghost")


class TestResolveAndVerifyPoint:
    async def test_resolve_point_returns_configured_address(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        result = await modbus_runtime.rt.diagnostic.resolve_point(
            "modbus-1", "rotor.speed"
        )
        assert result.point_id == "rotor.speed"
        assert result.configured_address["address"] == 100
        assert result.configured_address["register_type"] == "holding"
        # Modbus 无协议侧解析：resolved 即配置地址事实。
        assert result.resolved_address is not None

    async def test_verify_point_reports_raw_and_engineering_value(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        # gen.power: raw 800.0，scale=2.0 offset=10.0 → engineering 1610.0。
        result = await modbus_runtime.rt.diagnostic.verify_point("modbus-1", "gen.power")
        assert result.ok is True
        assert result.readable is True
        assert result.raw_value == pytest.approx(800.0)
        assert result.engineering_value == pytest.approx(1610.0)

    async def test_verify_point_read_failure_marks_point_unreadable(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        server = modbus_runtime.server
        assert server is not None
        await server.stop()
        result = await modbus_runtime.rt.diagnostic.verify_point(
            "modbus-1", "rotor.speed"
        )
        # 诊断必须把读失败内联到结果而非抛出。
        assert result.ok is False
        assert result.readable is False
        assert result.error

    async def test_verify_point_unknown_point(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        with pytest.raises(CommandError, match="unknown point"):
            await modbus_runtime.rt.diagnostic.verify_point("modbus-1", "ghost.point")


class TestVerifyPoints:
    async def test_verify_all_points_in_group(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        result = await modbus_runtime.rt.diagnostic.verify_points(
            "modbus-1", point_group="telemetry"
        )
        # telemetry 组含 rotor.speed / gen.power / temp.int / setpoint.power。
        assert result.checked == 4
        assert result.passed == 4
        assert result.failed == 0
        assert result.ok is True

    async def test_verify_points_without_group_covers_full_table(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        result = await modbus_runtime.rt.diagnostic.verify_points("modbus-1")
        assert result.point_group is None
        assert result.checked == 4
        assert result.ok is True

    async def test_verify_points_unknown_group_raises(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        with pytest.raises(CommandError, match="unknown or empty point group"):
            await modbus_runtime.rt.diagnostic.verify_points(
                "modbus-1", point_group="ghost"
            )

    async def test_verify_points_partial_failure_aggregates(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        server = modbus_runtime.server
        assert server is not None
        await server.stop()
        result = await modbus_runtime.rt.diagnostic.verify_points(
            "modbus-1", point_group="telemetry"
        )
        assert result.checked == 4
        assert result.passed == 0
        assert result.failed == 4
        assert result.ok is False
