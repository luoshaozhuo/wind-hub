"""Recovery：短周期故障/重载循环的资源稳定性——不得单调泄漏。

与 soak 的长时资源采样的区别：这里针对的是故障注入路径本身
（reconnect 循环、配置收敛循环、Sink 消费协程重建）——这些路径涉及
连接重建、协议实例替换、acquisition handle 重启、sink consumer 重建，
是句柄泄漏的高发区。循环次数有限（10 次），断言的是「不单调增长」
而非长时平稳。

v2 拓扑：Collector 与 Sink Service 是两个独立进程，因此 RES-02 分别
在两个进程边界采样 FD——Collector 侧通过新版本 task/model 模板 +
devices 文档替换驱动采集句柄重建，Sink 侧通过 RestartSink 管理面
驱动 consumer 协程重建。

观测点（全部在进程边界）：
- ``/proc/<pid>/fd`` 采样（含 socket/文件/管道）；
- v2 ListTasks 的设备状态表（每设备唯一 Task，无重复 handle）；
- GetMetrics 的 reconnects 计数（确认故障循环真实发生）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from sink_service.management_cli import request as sink_mgmt
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import (
    instance_states,
    metrics_snapshot,
    replace_config,
    replace_template,
    start_task_instance,
)
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    wait_status,
)
from tests.support.redis_client import point_key
from tests.support.v2_config import emit_v2_tree_from_flat_site
from tests.support.wait import wait_redis_value, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID

CYCLES = 10
#: FD 采样容忍的瞬时波动（日志 flush、metrics 连接等）；泄漏是单调增长，
#: 10 次循环后仍落在基线 +3 以内即不成立。
FD_SLACK = 3

KEY_PREFIX = "wind-hub-resources"


def _flat_config(base: Path, device_port: int, redis_address: str, nats_url: str) -> Path:
    host, _, port = redis_address.rpartition(":")
    return write_functional_config(
        base,
        device_port,
        nats_url=nats_url,
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {"host": host, "port": int(port)},
                "outputs": {TASK_ID: {"key_prefix": KEY_PREFIX}},
            }
        },
        tasks={
            TASK_ID: {
                "device_group_id": DEVICE_ID,
                "point_group": "telemetry",
                "interval": 0.2,
            }
        },
    )


async def _wait_data(address: str, *, after: str | None = None) -> str:
    key = point_key(DEVICE_ID, "rotor_speed", prefix=KEY_PREFIX)
    value = await wait_redis_value(
        address,
        key,
        match=(lambda payload: str(payload["timestamp"]) > after) if after else None,
    )
    return str(value["timestamp"])


async def _single_running_instance(proc: CollectorProcess) -> None:
    """断言本用例设备唯一且处于 RUNNING 生命周期状态（轮询等待恢复收敛）。"""

    async def _probe() -> bool | None:
        states = await instance_states(proc.grpc_target)
        if states is None:
            return None
        running = states.get(f"{TASK_ID}:{INSTANCE_ID}") == "RUNNING"
        if running and len(states) == 1:
            return True
        return None

    await wait_until(_probe, timeout=20.0, description="single RUNNING instance after recovery")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _model_ref(v2_tree: Path) -> tuple[str, str, str, str]:
    """返回 (model_id, model_version, task_id, task_version) 当前引用链。"""
    devices = _load(v2_tree / "instances" / "devices.yaml")
    model = devices["devices"][DEVICE_ID]["model"]
    model_path = v2_tree / "templates" / "device_models" / model["id"] / f"{model['version']}.yaml"
    model_doc = _load(model_path)
    return model["id"], model["version"], model_doc["task"]["id"], model_doc["task"]["version"]


async def _bump_task_interval(
    proc: CollectorProcess,
    v2_tree: Path,
    *,
    cycle: int,
    interval: float,
) -> None:
    """新版本 task + model 模板，再把 devices 文档引用切到新版本。

    v2 模板不可变：节拍变更必须发布新版本，不能覆盖旧版本文件。
    """
    model_id, _, task_template_id, task_version = _model_ref(v2_tree)
    task_path = v2_tree / "templates" / "tasks" / task_template_id / f"{task_version}.yaml"
    task_doc = _load(task_path)
    new_task_version = f"1.0.{cycle + 1}"
    new_task_doc = copy.deepcopy(task_doc)
    new_task_doc["template_version"] = new_task_version
    new_task_doc["interval"] = interval
    await replace_template(
        proc.grpc_target, "tasks", task_template_id, new_task_version, new_task_doc
    )

    model_version = _model_ref(v2_tree)[1]
    model_path = v2_tree / "templates" / "device_models" / model_id / f"{model_version}.yaml"
    model_doc = _load(model_path)
    new_model_version = f"1.0.{cycle + 1}"
    new_model_doc = copy.deepcopy(model_doc)
    new_model_doc["template_version"] = new_model_version
    new_model_doc["task"] = {"id": task_template_id, "version": new_task_version}
    await replace_template(
        proc.grpc_target, "device_models", model_id, new_model_version, new_model_doc
    )

    devices_path = v2_tree / "instances" / "devices.yaml"
    devices_doc = _load(devices_path)
    devices_doc["devices"][DEVICE_ID]["model"]["version"] = new_model_version
    result = await replace_config(
        proc.grpc_target,
        yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False),
    )
    assert result.success, f"cycle {cycle} devices replace failed: {list(result.errors)}"


class TestReconnectFlapStability:
    async def test_ten_disconnect_cycles_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RES-01：10 次断连/恢复循环后实例唯一、FD 不增长。"""
        flat_dir = _flat_config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        v2_tree = emit_v2_tree_from_flat_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await _wait_data(redis_service)

        baseline_reconnects = int(
            (await metrics_snapshot(proc.grpc_target))["device_reconnects"].get(DEVICE_ID, 0)
        )
        baseline_fd = proc.fd_count()

        for cycle in range(CYCLES):
            await modbus_server.stop()
            await wait_status(
                proc,
                lambda p: p["device_connected"] == 0,
                description=f"cycle {cycle}: disconnected",
            )
            await modbus_server.start()
            await wait_status(
                proc,
                lambda p: p["device_connected"] == 1,
                description=f"cycle {cycle}: reconnected",
            )
            await _single_running_instance(proc)

        # 数据面在最后一次恢复后继续出数。
        previous = await _wait_data(redis_service)
        await _wait_data(redis_service, after=previous)

        reconnects = int(
            (await metrics_snapshot(proc.grpc_target))["device_reconnects"].get(DEVICE_ID, 0)
        )
        assert (
            reconnects - baseline_reconnects >= CYCLES
        ), f"expected >= {CYCLES} reconnects, got {reconnects - baseline_reconnects}"
        final_fd = proc.fd_count()
        assert (
            final_fd <= baseline_fd + FD_SLACK
        ), f"fd count grew from {baseline_fd} to {final_fd} over {CYCLES} flap cycles"


class TestReloadStability:
    async def test_ten_reloads_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RES-02：10 轮配置收敛（Collector 采集句柄重建）+ Sink 重启
        （consumer 协程重建）后，两侧实例/句柄唯一、FD 不增长。"""
        flat_dir = _flat_config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        sink = await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        # 模板新版本发布须显式开启发布闸门（生产 fail-closed 开关）。
        proc: CollectorProcess = await collector_factory(
            v2_tree, env_extra={"WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH": "enabled"}
        )
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await _wait_data(redis_service)
        collector_fd = proc.fd_count()
        sink_fd = sink.fd_count()

        for cycle in range(CYCLES):
            interval = 0.2 if cycle % 2 == 0 else 0.3

            # Collector 侧：新版本 task/model + devices 引用切换，
            # 强制采集句柄按新节拍重建。
            await _bump_task_interval(proc, v2_tree, cycle=cycle, interval=interval)

            # Sink 侧：管理面重启 Redis Sink，consumer 协程整体替换。
            restarted = await sink_mgmt(sink.grpc_target, "RestartSink", "redis_sink")
            assert restarted.get("state") == "running", restarted

            await _single_running_instance(proc)

        # 采集在最后一轮配置下继续出数。
        previous = await _wait_data(redis_service)
        await _wait_data(redis_service, after=previous)

        final_collector_fd = proc.fd_count()
        assert final_collector_fd <= collector_fd + FD_SLACK, (
            f"collector fd grew from {collector_fd} to {final_collector_fd} "
            f"over {CYCLES} reloads"
        )
        final_sink_fd = sink.fd_count()
        assert (
            final_sink_fd <= sink_fd + FD_SLACK
        ), f"sink fd grew from {sink_fd} to {final_sink_fd} over {CYCLES} sink restarts"
