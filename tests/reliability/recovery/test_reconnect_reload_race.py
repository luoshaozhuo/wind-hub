"""Recovery：reconnect/backoff 中途叠加配置变更与任务控制的组合故障。

这些场景与普通 reconnect 的本质区别：设备处于 disconnected/backoff 状态
时 Runtime 内部正有重连活动，配置事务（rebuild）与任务控制（stop）必须
与这条异步路径正确交互——设备恢复后不得复活旧点表、旧实例或已被明确
停止的 Task。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import (
    ctl_instance_states,
    ctl_status,
    wait_status,
    write_modbus_file_config,
)
from tests.support.control import (
    apply_placement_and_start_instance,
    reload_config,
    stop_instance,
)
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess
from tests.support.wait import read_jsonl, wait_file_rows, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"

# gen.power 默认 scale=2.0/offset=10.0：raw 800.0 → 1610.0；去掉换算后 → 800.0。
SCALED_POWER = 1610.0
RAW_POWER = 800.0


class TestPointTableChangeWhileOffline:
    async def test_new_point_table_applies_after_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """CFG-02：掉线期间改点表——设备恢复后必须使用新点表出数。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(
            sink_path, min_rows=1, match=lambda r: r["value"] == pytest.approx(SCALED_POWER)
        )

        await modbus_server.stop()
        await wait_status(
            proc, lambda p: p["devices_connected"] == 0, description="device disconnected"
        )

        # ---- 掉线期间修改点表（gen.power 去掉工程值换算）并重载 ----
        def _drop_scaling(data: dict) -> None:
            for point in data["point_tables"]["modbus"]["points"]:
                if point["point_id"] == "gen.power":
                    point.pop("scale", None)
                    point.pop("offset", None)

        update_yaml(config_dir, "points.yaml", _drop_scaling)
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-raw-power"
        )
        assert activated.success, f"activate failed: {list(activated.errors)}"

        # reload 不改变连接事实：设备仍是离线。
        status = await ctl_status(proc)
        assert status is not None
        assert status["devices_connected"] == 0

        # ---- 设备恢复：sink 必须输出新点表语义（raw 800.0），不得复活旧换算 ----
        await modbus_server.start()
        await wait_status(
            proc, lambda p: p["devices_connected"] == 1, description="device reconnected"
        )
        await wait_file_rows(
            sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(RAW_POWER)
        )
        await asyncio.sleep(1.0)
        scaled = [
            r for r in read_jsonl(sink_path) if r["value"] == pytest.approx(SCALED_POWER)
        ]
        settled = len(scaled)
        await asyncio.sleep(1.0)
        scaled = [
            r for r in read_jsonl(sink_path) if r["value"] == pytest.approx(SCALED_POWER)
        ]
        assert len(scaled) == settled, "stale point table still in use after recovery"


class TestStopTaskDuringReconnect:
    async def test_task_stays_stopped_after_device_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """CFG-03：backoff 期间 stop task——设备恢复后 Task 必须保持 STOPPED。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=2)

        await modbus_server.stop()
        await wait_status(
            proc, lambda p: p["devices_connected"] == 0, description="device disconnected"
        )

        # ---- reconnect/backoff 进行中停止实例（断言生命周期状态，非 collect 瞬态） ----
        await stop_instance(proc.grpc_target, INSTANCE_ID)

        async def _stopped() -> bool | None:
            states = await ctl_instance_states(proc)
            if states is None:
                return None
            return True if states.get(INSTANCE_ID) == "stopped" else None

        await wait_until(_stopped, timeout=15.0, description="instance STOPPED")

        # ---- 设备恢复：reconnect 成功不得顺带把实例拉起来 ----
        await modbus_server.start()
        await asyncio.sleep(3.0)  # 覆盖至少一次重连成功 + 若干采集周期

        states = await ctl_instance_states(proc)
        assert states is not None
        assert states.get(INSTANCE_ID) == "stopped", (
            f"device recovery resurrected a stopped task: {states}"
        )

        # 数据面保持静默（等残余缓冲落盘后行数不再增长）。
        await asyncio.sleep(1.0)
        settled = len(read_jsonl(sink_path))
        await asyncio.sleep(1.0)
        assert len(read_jsonl(sink_path)) == settled
