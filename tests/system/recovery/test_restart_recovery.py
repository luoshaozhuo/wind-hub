"""Recovery：重启类恢复——Collector 进程重启恢复 + 断连期间配置发布的恢复。

- TestCollectorRestart：优雅停机后同配置重新拉起，控制面/数据面完整恢复；
- TestPointTablePublishedWhileDisconnected：IEC104 设备断连期间发布新点表
  版本（不可变模板新版本 + devices 引用切换）。v2 契约（G-2）：配置替换
  立即持久化并记录，设备不可达不允许让替换失败；驱动重连后按 stored
  代际收敛，新点在重连后自动到达，不需要再发第二次「reload 重试」。
"""

from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from contracts import collector_pb2, collector_pb2_grpc
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.collector_config_tree import build_collector_tree_from_site
from tests.support.config_helper import write_config_tree
from tests.support.control import (
    collector_info,
    get_disk_yaml,
    get_task,
    replace_catalog,
    replace_config,
    replace_template,
    start_task_instance,
)
from tests.support.functional_config import realtime_messaging
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    recovery_collector_tree,
    wait_status,
    wait_telemetry_value,
    write_modbus_redis_config,
)
from tests.support.sink_harness import latest_value_key
from tests.support.wait import wait_redis_value

pytestmark = [pytest.mark.redis, pytest.mark.real_service]

MODBUS_DEVICE_ID = "modbus-1"

IEC104_DEVICE_ID = "iec104-1"
IEC104_INSTANCE_ID = IEC104_DEVICE_ID

IEC104_POINTS: dict[str, dict[str, Any]] = {
    "meas_active_power": {
        "point_groups": ["telemetry"],
        "address": {"ca": 1, "ioa": 100, "type_id": "M_ME_NC_1"},
        "data_type": "float32",
    },
    "meas_frequency": {
        "point_groups": ["telemetry"],
        "address": {"ca": 1, "ioa": 200, "type_id": "M_ME_NC_1"},
        "data_type": "float32",
    },
}

#: 模板发布闸门：本模块用例会发布新点表/型号版本。
TEMPLATE_PUBLISH_ENV = {"WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH": "enabled"}


async def _wait_device_state(
    proc: CollectorProcess, device_id: str, expected: set[str], *, timeout: float = 15.0
) -> str:
    """轮询 ListDevices 直到设备 actual_state 进入期望集合。"""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = str((await _device_states(proc)).get(device_id, ""))
        if last in expected:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"{device_id} never reached {expected}; last={last}")


async def _device_states(proc: CollectorProcess) -> dict[str, str]:
    async with grpc.aio.insecure_channel(proc.grpc_target) as channel:
        stub = collector_pb2_grpc.CollectorServiceStub(channel)
        response = await stub.ListDevices(collector_pb2.ListDevicesRequest(), timeout=5.0)
    return {device.device_id: str(device.actual_state) for device in response.devices}


def _iec104_flat_config(base: Path, port: int, redis_address: str, nats_url: str) -> Path:
    """单 IEC104 设备 + Redis 最新值的扁平现场配置（订阅型采集）。"""
    return write_config_tree(
        base,
        devices={
            IEC104_DEVICE_ID: {
                "protocol": "iec104",
                "point_table": "iec104",
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": port,
                },
                "protocol_options": {
                    # 缩短协议定时器；断连后由 Watchdog 显式重连并恢复
                    # LISTEN + 总召（恢复阶段不依赖人工干预）。
                    "t0": 5.0,
                    "t1": 3.0,
                    "t2": 2.0,
                    "t3": 5.0,
                },
            }
        },
        point_tables={"iec104": {"points": copy.deepcopy(IEC104_POINTS)}},
        tasks={
            IEC104_DEVICE_ID: {
                "device_group_id": IEC104_DEVICE_ID,
                "point_group": "telemetry",
                # IEC104 为订阅型：interval 被协议忽略，仅满足 schema。
                "interval": 0.2,
            }
        },
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
                "watchdog_interval": 0.5,
                "health_timeout": 0.5,
            },
            "messaging": realtime_messaging(nats_url),
        },
    )


@pytest.mark.modbus
class TestCollectorRestart:
    async def test_graceful_restart_restores_service(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        flat_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        # sink_service 跨 Collector 重启独立存活，数据面恢复由它持续消费保证。
        await sink_service_factory(
            collector_id="recovery-restart", deployment_id="restart-modbus-sink"
        )
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)

        # ---- 第一个实例：正常采集后优雅停机 ----
        proc_a: CollectorProcess = await collector_factory(v2_tree, collector_id="recovery-restart")
        await start_task_instance(proc_a.grpc_target, instance_id=MODBUS_DEVICE_ID)
        first = await wait_telemetry_value(redis_service, collector_id="recovery-restart")
        exit_code = proc_a.terminate()
        assert exit_code == 0
        baseline = str(first["sample_time"])

        # ---- 同配置重启：控制面恢复、读可用、采集可重新启动 ----
        proc_b: CollectorProcess = await collector_factory(v2_tree, collector_id="recovery-restart")
        info = await collector_info(proc_b.grpc_target)
        assert info.process_role == "collector"
        assert info.service_id == "recovery-restart"

        await start_task_instance(proc_b.grpc_target, instance_id=MODBUS_DEVICE_ID)
        await wait_telemetry_value(redis_service, collector_id="recovery-restart", after=baseline)


@pytest.mark.iec104
class TestPointTablePublishedWhileDisconnected:
    async def test_new_template_version_converges_after_reconnect(
        self,
        iec104_server: IEC104MockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        flat_dir = _iec104_flat_config(
            tmp_path / "cfg", iec104_server.port, redis_service, nats_service
        )
        await sink_service_factory(
            namespace="wind-hub-iec-restart", deployment_id="restart-iec104-sink"
        )
        v2_tree = build_collector_tree_from_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(
            v2_tree, env_extra=dict(TEMPLATE_PUBLISH_ENV)
        )

        # ---- 正常：LISTEN 会话建立，自发上送写入 Redis ----
        # v1 契约：总招是显式 InterrogateDevice RPC，其 COT 只归当次请求，
        # 不进 LISTEN 采集；后台采集数据来自从站自发/周期上送（COT 1/2/3）。
        # 因此用 fixture 的 spontaneous 上送驱动初始数据。
        await start_task_instance(proc.grpc_target, instance_id=IEC104_INSTANCE_ID)
        key = latest_value_key(
            "wind-hub-iec-restart", device_id=IEC104_DEVICE_ID, point_id="meas_active_power"
        )
        await _wait_device_state(proc, IEC104_DEVICE_ID, {"STARTING", "RUNNING"})
        await iec104_server.set_value(100, 1500.5, spontaneous=True)
        initial = await wait_redis_value(redis_service, key)
        initial_ts = str(initial["sample_time"])

        # ---- 故障：从站停止，设备断连在边界可见 ----
        await iec104_server.stop()
        await wait_status(
            proc,
            lambda p: p["device_connected"] == 0,
            description="iec104 device detected disconnected",
        )

        # ---- 断连期间发布新点表版本（新增 meas_reactive_power）----
        # v2 模板不可变：业务点目录补登记、点表 1.0.1、型号 1.0.1 引用新点表，
        # devices 文档再把 model 引用切到 1.0.1；设备不可达不得使替换失败
        # （G-2 契约）。
        catalog = yaml.safe_load(
            (v2_tree / "templates" / "business_points_standard.yaml").read_text(encoding="utf-8")
        )
        catalog["business_points"]["meas_reactive_power"] = {
            "data_type": "float32",
            "unit": "none",
        }
        catalog_result = await replace_catalog(proc.grpc_target, "business_points", catalog)
        assert catalog_result.success, list(catalog_result.errors)

        old_table = yaml.safe_load(
            (v2_tree / "templates" / "point_tables" / "point_table_iec104_v1.0.0.yaml").read_text()
        )
        new_table = copy.deepcopy(old_table)
        new_table["template_version"] = "1.0.1"
        new_table["file_version"] = "1.0.1"
        new_table["points"]["meas_reactive_power"] = {
            "point_groups": ["telemetry", "all"],
            "address": {"ca": 1, "ioa": 300, "type_id": "M_ME_NC_1"},
            "data_type": "float32",
            "unit": "none",
        }
        table_result = await replace_template(
            proc.grpc_target, "point_tables", "iec104", "1.0.1", new_table
        )
        assert table_result.success, list(table_result.errors)

        # 候选从已提交代际派生：启动源 YAML 早于 StartTask 的持久 START
        # 意图，直接替换会静默重置为 STOP（RES-02）。
        devices_doc = await get_disk_yaml(proc.grpc_target, "instances/devices.yaml")
        assert (
            devices_doc["devices"][IEC104_DEVICE_ID]["overrides"]["task"]["desired_state"]
            == "START"
        )
        model_id = devices_doc["devices"][IEC104_DEVICE_ID]["model"]["id"]
        old_model = yaml.safe_load(
            (
                v2_tree / "templates" / "device_models" / f"device_model_{model_id}_v1.0.0.yaml"
            ).read_text()
        )
        new_model = copy.deepcopy(old_model)
        new_model["template_version"] = "1.0.1"
        new_model["file_version"] = "1.0.1"
        new_model["point_table"] = {"id": "iec104", "version": "1.0.1"}
        model_result = await replace_template(
            proc.grpc_target, "device_models", model_id, "1.0.1", new_model
        )
        assert model_result.success, list(model_result.errors)

        devices_doc["devices"][IEC104_DEVICE_ID]["model"]["version"] = "1.0.1"
        devices_result = await replace_config(
            proc.grpc_target,
            yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False),
        )
        assert devices_result.success, list(devices_result.errors)

        # 现场配套：从站固件升级新增 IOA 300 测点（停机期注册，重启装配）。
        await iec104_server.add_point(300, 77.5)

        # 配置已持久化收敛不改变连接事实：设备仍离线，进程存活、任务仍登记。
        await wait_status(
            proc, lambda p: p["device_connected"] == 0, description="still disconnected"
        )
        assert proc.is_running()
        task = await get_task(proc.grpc_target, IEC104_DEVICE_ID)
        assert task.device_id == IEC104_DEVICE_ID

        # ---- 恢复：从站重启，驱动自动重连，stored 代际在新连接上生效 ----
        await iec104_server.start()
        # IEC104 无总招自动数据：自发上送驱动新值（新点表会话按 1.0.1 生效，
        # meas_reactive_power 已进入 LISTEN 点集）。
        await _wait_device_state(proc, IEC104_DEVICE_ID, {"STARTING", "RUNNING"}, timeout=60.0)
        await iec104_server.set_value(100, 1600.5, spontaneous=True)
        await wait_redis_value(
            redis_service,
            key,
            timeout=30.0,
            match=lambda value: str(value["sample_time"]) > initial_ts,
        )
        # 新点表无需第二次 reload：新点在重连后同样经 LISTEN 出数。
        await iec104_server.set_value(300, 77.5, spontaneous=True)
        new_key = latest_value_key(
            "wind-hub-iec-restart", device_id=IEC104_DEVICE_ID, point_id="meas_reactive_power"
        )
        new_value = await wait_redis_value(redis_service, new_key, timeout=30.0)
        assert new_value["value"] == pytest.approx(77.5)
