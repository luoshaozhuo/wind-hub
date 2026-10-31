"""Recovery：reconnect/backoff 中途叠加配置变更与任务控制的组合故障。

这些场景与普通 reconnect 的本质区别：设备处于 disconnected/backoff 状态
时 Runtime 内部正有重连活动，配置收敛（设备文档替换）与任务控制（stop）
必须与这条异步路径正确交互——设备恢复后不得复活旧点表、旧实例或已被明确
停止的 Task。

v2 下点表变更 = 不可变模板新版本（点表 1.0.1 + 型号 1.0.1）+ devices
文档引用切换；任务以设备为粒度（task_id == device_id）。
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import (
    instance_states,
    replace_config,
    replace_template,
    start_task_instance,
    stop_task,
)
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    status_snapshot,
    wait_status,
    wait_telemetry_value,
    write_modbus_redis_config,
)
from tests.support.wait import wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID
#: instance_states 的键习惯为 "{task_id}:{device_id}"；v2 两者相同。
STATE_KEY = f"{TASK_ID}:{DEVICE_ID}"

# gen_power 默认 scale=2.0/offset=10.0：raw 800.0 → 1610.0；去掉换算后 → 800.0。
SCALED_POWER = 1610.0
RAW_POWER = 800.0

TEMPLATE_PUBLISH_ENV = {"WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH": "enabled"}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


async def _publish_raw_power_table(proc: CollectorProcess, v2_tree: Path) -> None:
    """掉线期间发布去掉 scale/offset 的点表 1.0.1 + 型号 1.0.1 并切换引用。"""
    devices_doc = _load(v2_tree / "instances" / "devices.yaml")
    model_id = devices_doc["devices"][DEVICE_ID]["model"]["id"]

    table_doc = _load(v2_tree / "templates" / "point_tables" / "modbus" / "1.0.0.yaml")
    new_table = copy.deepcopy(table_doc)
    new_table["template_version"] = "1.0.1"
    point = new_table["points"]["gen_power"]
    point.pop("scale", None)
    point.pop("offset", None)
    table_result = await replace_template(
        proc.grpc_target, "point_tables", "modbus", "1.0.1", new_table
    )
    assert table_result.success, list(table_result.errors)

    model_doc = _load(v2_tree / "templates" / "device_models" / model_id / "1.0.0.yaml")
    new_model = copy.deepcopy(model_doc)
    new_model["template_version"] = "1.0.1"
    new_model["point_table"] = {"id": "modbus", "version": "1.0.1"}
    model_result = await replace_template(
        proc.grpc_target, "device_models", model_id, "1.0.1", new_model
    )
    assert model_result.success, list(model_result.errors)

    devices_doc["devices"][DEVICE_ID]["model"]["version"] = "1.0.1"
    result = await replace_config(
        proc.grpc_target,
        yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False),
    )
    assert result.success, list(result.errors)


class TestPointTableChangeWhileOffline:
    async def test_new_point_table_applies_after_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """CFG-02：掉线期间发布新点表版本——设备恢复后必须使用新点表出数。"""
        flat_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(
            v2_tree, env_extra=dict(TEMPLATE_PUBLISH_ENV)
        )
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_telemetry_value(
            redis_service,
            point_id="gen_power",
            match=lambda value: value["value"] == pytest.approx(SCALED_POWER),
        )

        await modbus_server.stop()
        await wait_status(
            proc, lambda p: p["device_connected"] == 0, description="device disconnected"
        )

        # ---- 掉线期间发布新点表版本（gen_power 去掉工程值换算）----
        await _publish_raw_power_table(proc, v2_tree)

        # 配置收敛不改变连接事实：设备仍是离线。
        status = await status_snapshot(proc)
        assert status is not None
        assert status["device_connected"] == 0

        # ---- 设备恢复：sink 必须输出新点表语义（raw 800.0），不得复活旧换算 ----
        await modbus_server.start()
        await wait_status(
            proc, lambda p: p["device_connected"] == 1, description="device reconnected"
        )
        observed = await wait_telemetry_value(
            redis_service,
            point_id="gen_power",
            match=lambda value: value["value"] == pytest.approx(RAW_POWER),
        )
        await asyncio.sleep(1.0)
        latest = await wait_telemetry_value(
            redis_service, point_id="gen_power", after=str(observed["timestamp"])
        )
        assert latest["value"] == pytest.approx(RAW_POWER)


class TestStopTaskDuringReconnect:
    async def test_task_stays_stopped_after_device_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """CFG-03：backoff 期间 STOP 设备——设备恢复后 Task 必须保持 STOPPED。"""
        flat_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_telemetry_value(redis_service)

        await modbus_server.stop()
        await wait_status(
            proc, lambda p: p["device_connected"] == 0, description="device disconnected"
        )

        # ---- reconnect/backoff 进行中下发设备级 STOP ----
        await stop_task(proc.grpc_target, DEVICE_ID)

        async def _stopped() -> bool | None:
            states = await instance_states(proc.grpc_target)
            if states is None:
                return None
            return True if states.get(STATE_KEY) == "STOPPED" else None

        await wait_until(_stopped, timeout=15.0, description="task STOPPED")

        # ---- 设备恢复：reconnect 成功不得顺带把任务拉起来 ----
        await modbus_server.start()
        await asyncio.sleep(3.0)  # 覆盖至少一次重连成功 + 若干采集周期

        states = await instance_states(proc.grpc_target)
        assert states is not None
        assert (
            states.get(STATE_KEY) == "STOPPED"
        ), f"device recovery resurrected a stopped task: {states}"

        # 停机后确认数据静默。
        settled = await wait_telemetry_value(redis_service)
        await asyncio.sleep(1.0)
        final = await wait_telemetry_value(redis_service)
        assert final["timestamp"] == settled["timestamp"]
