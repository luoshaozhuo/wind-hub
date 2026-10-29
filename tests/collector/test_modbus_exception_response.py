"""Component：Modbus 异常响应（Illegal Data Address）的运行时分类与恢复。

异常响应表示「链路活着、对端明确拒绝这条请求」——几乎都是点表配置
错误（本文件用 fixture server 寄存器块之外的地址 300 注入）。Runtime
必须将其分类为**非连接级**失败：不标记断连、不触发重连、不进入退避。
否则一张错点表会把设备打入断连-重连循环，淹没真正离线的设备。

修复点表并 reload 后，同一条连接上采集必须恢复（全程零重连）。

Sink Service 独立服务化后，「数据最终写入 Redis」的端到端恢复验证属于
Collector→NATS→Sink Service 系统链路，见 tests/system 下的 E2E 测试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.support.functional_config import (
    MODBUS_POINTS,
    update_yaml,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.modbus,
]
POINTS = "points.yaml"

#: fixture server 保持寄存器块为 256 个（wire 0..255）——地址 300 必然
#: 触发 Illegal Data Address 异常响应。
BAD_POINT: dict[str, Any] = {
    "point_groups": ["telemetry"],
    "address": {"register_type": "holding", "address": 300},
    "data_type": "int16",
}


def _points_with_bad_address() -> dict[str, Any]:
    return {"modbus": {"points": {**MODBUS_POINTS, "bad_address": dict(BAD_POINT)}}}


def _telemetry_task() -> dict[str, Any]:
    """Task 不再声明 outputs；Sink 绑定属于 sink_service 的 sinks.yaml。"""
    return {
        "modbus-telemetry": {
            "device_group_id": "modbus-1",
            "point_group": "telemetry",
            "interval": 0.2,
        }
    }


class TestExceptionResponseClassification:
    async def test_exception_response_not_classified_as_disconnect(
        self, app_factory, tmp_path: Path
    ) -> None:
        ctx = await app_factory(
            point_tables=_points_with_bad_address(),
            sinks={},
            tasks=_telemetry_task(),
        )
        await ctx.start_instances()
        app = ctx.app
        base = app.metrics.snapshot()

        # 等待首个采集周期完成：连接建立但尚未通信时 health 不声称健康
        # （传输打开 ≠ 设备可应答），首次真实通信后异常响应计为链路健康。
        protocol = app.runtime.devices["modbus-1"].protocol
        for _ in range(50):
            if (await protocol.health()).healthy:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("首个采集周期后 health 仍未反映通信结果")

        # 3s（约 15 个采集周期）内持续采样：连接必须保持。
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            protocol = app.runtime.devices["modbus-1"].protocol
            assert (
                await protocol.health()
            ).healthy is True, "异常响应被误判为断连（driver health 掉线）"
            state = app.runtime.device_state("modbus-1")
            assert (
                state is not None and state.connected is True
            ), "异常响应被误判为断连（runtime state 掉线）"
            await asyncio.sleep(0.3)

        snap = app.metrics.snapshot()
        assert snap["device_reconnects"].get("modbus-1", 0) == 0
        assert snap["device_connect_failures"].get("modbus-1", 0) == base[
            "device_connect_failures"
        ].get("modbus-1", 0), "异常响应不得触发重连"
        # 引擎没有被异常响应卡死：周期继续推进、失败被如实计数。
        assert snap["counters"]["acquisition_runs"] - base["counters"]["acquisition_runs"] >= 5
        assert (
            snap["counters"]["acquisition_failures"] - base["counters"]["acquisition_failures"] >= 5
        ), "异常响应导致的采集失败必须可见，不得静默吞掉"

    async def test_fixing_point_table_recovers_without_reconnect(
        self, app_factory, tmp_path: Path
    ) -> None:
        ctx = await app_factory(
            point_tables=_points_with_bad_address(),
            sinks={},
            tasks=_telemetry_task(),
        )
        await ctx.start_instances()
        app = ctx.app
        await asyncio.sleep(1.0)  # 故障点表不产生有效采集

        # ---- 修复点表（去掉非法地址点）并 reload ----
        def mutate(data: dict[str, Any]) -> None:
            data["point_tables"]["modbus"]["points"].pop("bad_address", None)

        update_yaml(ctx.config_dir, POINTS, mutate)
        result = await app.config.reload()
        assert result.success, result.errors

        # ---- 同一条连接上采集恢复，全程零重连 ----
        protocol = app.runtime.devices["modbus-1"].protocol
        deadline = asyncio.get_running_loop().time() + 5.0
        good_state_seen = 0
        while asyncio.get_running_loop().time() < deadline:
            state = app.runtime.device_state("modbus-1")
            if state is not None and state.connected:
                good_state_seen += 1
            await asyncio.sleep(0.1)
        assert good_state_seen > 0, "修复点表后采集状态未恢复"
        snap = app.metrics.snapshot()
        assert snap["device_reconnects"].get("modbus-1", 0) == 0
        assert (await protocol.health()).healthy is True
