"""Component：Modbus 异常响应（Illegal Data Address）的运行时分类与恢复。

异常响应表示「链路活着、对端明确拒绝这条请求」——几乎都是点表配置
错误（本文件用 fixture server 寄存器块之外的地址 300 注入）。Runtime
必须将其分类为**非连接级**失败：不标记断连、不触发重连、不进入退避。
否则一张错点表会把设备打入断连-重连循环，淹没真正离线的设备。

修复点表并 reload 后，同一条连接上数据面必须恢复（全程零重连）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.component.collector.conftest import (
    MODBUS_POINTS,
    FunctionalContext,
    update_yaml,
)
from tests.support.wait import read_jsonl, wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

POINTS = "points.yaml"

#: fixture server 保持寄存器块为 256 个（wire 0..255）——地址 300 必然
#: 触发 Illegal Data Address 异常响应。
BAD_POINT: dict[str, Any] = {
    "point_id": "bad.address",
    "point_groups": ["telemetry"],
    "address": {"register_type": "holding", "address": 300},
    "data_type": "int16",
}


def _points_with_bad_address() -> dict[str, Any]:
    return {"modbus": {"points": [*MODBUS_POINTS, dict(BAD_POINT)]}}


async def _start_instances(ctx: FunctionalContext) -> None:
    for instance in await ctx.rt.tasks.list_instances():
        await ctx.rt.tasks.start_instance(instance.instance_id)


def _file_sink(tmp_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path]:
    sink_path = tmp_path / "out" / "telemetry.jsonl"
    sinks = [
        {
            "name": "file_sink",
            "type": "file",
            "connection": {
                "path": str(sink_path),
                "buffer_size": 4,
                "flush_interval": 0.5,
            },
        }
    ]
    tasks = [
        {
            "task_id": "modbus-telemetry",
            "device": "modbus-1",
            "point_group": "telemetry",
            "interval": 0.2,
            "targets": [{"sink": "file_sink"}],
        }
    ]
    return sinks, tasks, sink_path


class TestExceptionResponseClassification:
    async def test_exception_response_not_classified_as_disconnect(
        self, runtime_factory, tmp_path: Path
    ) -> None:
        sinks, tasks, _ = _file_sink(tmp_path)
        async with runtime_factory(
            point_tables=_points_with_bad_address(), sinks=sinks, tasks=tasks
        ) as ctx:
            await _start_instances(ctx)
            base = ctx.rt.metrics_state.snapshot()

            # 3s（约 15 个采集周期）内持续采样：连接必须保持。
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                protocol = ctx.rt.runtime.devices["modbus-1"].protocol
                assert protocol.health().healthy is True, (
                    "异常响应被误判为断连（driver health 掉线）"
                )
                state = ctx.rt.runtime.device_state("modbus-1")
                assert state is not None and state.connected is True, (
                    "异常响应被误判为断连（runtime state 掉线）"
                )
                await asyncio.sleep(0.3)

            snap = ctx.rt.metrics_state.snapshot()
            assert snap["device_reconnects"].get("modbus-1", 0) == 0
            assert (
                snap["device_connect_failures"].get("modbus-1", 0)
                == base["device_connect_failures"].get("modbus-1", 0)
            ), "异常响应不得触发重连"
            # 引擎没有被异常响应卡死：周期继续推进、失败被如实计数。
            assert (
                snap["counters"]["acquisition_runs"] - base["counters"]["acquisition_runs"]
                >= 5
            )
            assert (
                snap["counters"]["acquisition_failures"]
                - base["counters"]["acquisition_failures"]
                >= 5
            ), "异常响应导致的采集失败必须可见，不得静默吞掉"

    async def test_fixing_point_table_recovers_without_reconnect(
        self, runtime_factory, tmp_path: Path
    ) -> None:
        sinks, tasks, sink_path = _file_sink(tmp_path)
        async with runtime_factory(
            point_tables=_points_with_bad_address(), sinks=sinks, tasks=tasks
        ) as ctx:
            await _start_instances(ctx)
            await asyncio.sleep(1.0)  # 确认故障配置下无数据落盘
            assert not sink_path.exists() or len(read_jsonl(sink_path)) == 0

            # ---- 修复点表（去掉非法地址点）并 reload ----
            def mutate(data: dict[str, Any]) -> None:
                data["point_tables"]["modbus"]["points"] = [
                    p for p in data["point_tables"]["modbus"]["points"]
                    if p["point_id"] != "bad.address"
                ]

            update_yaml(ctx.config_dir, POINTS, mutate)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors

            # ---- 同一条连接上数据面恢复，全程零重连 ----
            await wait_file_rows(sink_path, min_rows=2, timeout=10.0)
            snap = ctx.rt.metrics_state.snapshot()
            assert snap["device_reconnects"].get("modbus-1", 0) == 0
