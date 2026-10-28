"""Recovery：重启类恢复——Collector 进程重启恢复 + 订阅采集 reload 重试恢复。

- TestCollectorRestart：优雅停机后同配置重新拉起，控制面/数据面完整恢复；
- TestSubscriptionRestartPending：IEC104 订阅采集在设备断连期间 reload
  失败（实例进入 restart-pending），设备恢复后下一次 reload 自动重试
  成功——验证「reload 失败不让实例永久 STOPPED」的生产语义。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import (
    wait_status,
    wait_telemetry_value,
    write_modbus_redis_config,
)
from tests.support.config_helper import write_config_tree
from tests.support.control import (
    reload_config,
    start_task_instance,
)
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess, run_cli_async
from tests.support.redis_client import point_key
from tests.support.wait import wait_redis_value

pytestmark = [pytest.mark.redis, pytest.mark.real_service]

MODBUS_TASK_ID = "modbus-telemetry"
MODBUS_INSTANCE_ID = "modbus-telemetry:modbus-1"
IEC104_TASK_ID = "iec104-telemetry"
IEC104_INSTANCE_ID = "iec104-telemetry:iec104-1"

IEC104_POINTS: dict[str, dict[str, Any]] = {
    "meas.active_power": {
        "point_groups": ["telemetry"],
        "address": {"ca": 1, "ioa": 100, "type_id": "M_ME_NC_1"},
        "data_type": "float32",
    },
    "meas.frequency": {
        "point_groups": ["telemetry"],
        "address": {"ca": 1, "ioa": 200, "type_id": "M_ME_NC_1"},
        "data_type": "float32",
    },
}


def _iec104_config(base: Path, port: int, redis_address: str) -> Path:
    """单 IEC104 设备 + Redis 最新值的现场配置（订阅型采集）。"""
    return write_config_tree(
        base,
        devices={
            "iec104-1": {
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
        point_tables={"iec104": {"points": dict(IEC104_POINTS)}},
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {
                    "host": redis_address.rpartition(":")[0],
                    "port": int(redis_address.rpartition(":")[2]),
                },
            }
        },
        tasks={
            "iec104-telemetry": {
                "device_group_id": "iec104-1",
                "point_group": "telemetry",
                # IEC104 为订阅型：interval 被协议忽略，仅满足 schema。
                "interval": 0.2,
                "outputs": [{"sink_id": "redis_sink", "key_prefix": "wind-hub-iec-restart"}],
            }
        },
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
            }
        },
    )


@pytest.mark.modbus
class TestCollectorRestart:
    async def test_graceful_restart_restores_service(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service
        )

        # ---- 第一个实例：正常采集后优雅停机 ----
        proc_a: CollectorProcess = await collector_factory(
            config_dir, collector_id="recovery-restart"
        )
        await start_task_instance(
            proc_a.grpc_target,
            task_id=MODBUS_TASK_ID,
            instance_id=MODBUS_INSTANCE_ID,
        )
        first = await wait_telemetry_value(redis_service)
        exit_code = proc_a.terminate()
        assert exit_code == 0
        baseline = str(first["timestamp"])

        # ---- 同配置重启：控制面恢复、读可用、采集可重新启动 ----
        proc_b: CollectorProcess = await collector_factory(
            config_dir, collector_id="recovery-restart"
        )
        info = await run_cli_async("info", target=proc_b.grpc_target)
        assert info.returncode == 0
        assert json.loads(info.stdout)["collector_id"] == "recovery-restart"

        await start_task_instance(
            proc_b.grpc_target,
            task_id=MODBUS_TASK_ID,
            instance_id=MODBUS_INSTANCE_ID,
        )
        await wait_telemetry_value(redis_service, after=baseline)


@pytest.mark.iec104
class TestSubscriptionRestartPending:
    async def test_failed_subscription_restart_retried_on_next_reload(
        self,
        iec104_server: IEC104MockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = _iec104_config(tmp_path / "cfg", iec104_server.port, redis_service)
        proc: CollectorProcess = await collector_factory(config_dir)

        # ---- 正常：订阅建立，总召数据写入 Redis ----
        await start_task_instance(
            proc.grpc_target, task_id=IEC104_TASK_ID, instance_id=IEC104_INSTANCE_ID
        )
        key = point_key("iec104-1", "meas.active_power", prefix="wind-hub-iec-restart")
        await wait_redis_value(redis_service, key)

        # ---- 故障：从站停止，设备断连在边界可见 ----
        await iec104_server.stop()
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 0,
            description="iec104 device detected disconnected",
        )

        # ---- 断连期间变更点表：订阅必须重建但设备不可达 → reload 失败 ----
        def _add_point(data: dict[str, Any]) -> None:
            data["point_tables"]["iec104"]["points"]["meas.reactive_power"] = {
                "point_groups": ["telemetry"],
                "address": {"ca": 1, "ioa": 300, "type_id": "M_ME_NC_1"},
                "data_type": "float32",
                "unit": "none",
            }

        def _add_business_point(data: dict[str, Any]) -> None:
            data["business_points"]["meas.reactive_power"] = {
                "data_type": "float32",
                "unit": "none",
            }

        update_yaml(config_dir, "points.yaml", _add_point)
        update_yaml(config_dir, "business_points.yaml", _add_business_point)
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-disconnected"
        )
        assert activated.success is False
        assert activated.errors
        # 实例保持已登记（不是消失），进程存活。
        assert proc.is_running()
        instance = await run_cli_async("instance", IEC104_INSTANCE_ID, target=proc.grpc_target)
        assert instance.returncode == 0

        # ---- 恢复：从站重启，驱动自动重连 ----
        await iec104_server.start()
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=60.0,
            description="iec104 device reconnected",
        )

        # ---- 下一次 reload 重试 pending 的订阅重建：成功且数据恢复 ----
        baseline = str((await wait_redis_value(redis_service, key))["timestamp"])
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-recovered"
        )
        assert activated.success is True, list(activated.errors)

        instance = await run_cli_async("instance", IEC104_INSTANCE_ID, target=proc.grpc_target)
        assert json.loads(instance.stdout)["state"] == "running"
        await wait_redis_value(
            redis_service, key, timeout=30.0,
            match=lambda value: str(value["timestamp"]) > baseline,
        )
