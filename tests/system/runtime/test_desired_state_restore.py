"""Round 3 Runtime 验收：START/STOP 意图持久化与跨进程冷恢复。

v2 契约（每启用设备一个采集实例，task_id == device_id）：

- START/STOP 由 Collector 持久化到 devices.yaml 的
  ``overrides.task.desired_state``，不是内存标志；
- Collector 进程重启（优雅或强杀）后，冷启动必须按持久化意图自动恢复：
  上次为 START 的设备无需再发任何控制 RPC 即自动 RUNNING 并出数；
- 手动 STOP 是持久意图：设备重连、看门狗恢复、进程重启都不得复活任务；
- 观测全部在进程边界：Redis 最新值时间戳、v2 ListTasks 实际状态。

外部依赖：真实 Modbus fixture 从站、真实 NATS、真实 Redis、独立
sink_service 与 collector 子进程；不可用时由上层 fixture 明确 skip。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from contracts import collector_v2_pb2 as pb
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import list_tasks, start_task_instance, stop_task
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    wait_telemetry_value,
    write_modbus_redis_config,
)
from tests.support.wait import wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

COLLECTOR_ID = "runtime-restore"


async def _trees_and_sink(
    sink_service_factory, tmp_path: Path, port: int, redis_service: str, nats_service: str
):
    flat_dir = write_modbus_redis_config(
        tmp_path / "cfg", port, redis_service, nats_url=nats_service
    )
    await sink_service_factory(flat_dir, deployment_id="runtime-restore-sink")
    v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
    return v2_tree


async def _task_running(proc: CollectorProcess) -> bool | None:
    tasks = await list_tasks(proc.grpc_target, device_ids=(DEVICE_ID,))
    if not tasks:
        return None
    return tasks[0].actual_state == pb.RUNNING


async def _task_stopped(proc: CollectorProcess) -> bool | None:
    tasks = await list_tasks(proc.grpc_target, device_ids=(DEVICE_ID,))
    if not tasks:
        return None
    return tasks[0].actual_state == pb.STOPPED


class TestDesiredStateColdRestore:
    async def test_started_task_auto_runs_after_restart_without_rpc(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RT3-1：START 是持久意图——重启后零控制 RPC 自动恢复 RUNNING+出数。"""
        v2_tree = await _trees_and_sink(
            sink_service_factory, tmp_path, modbus_server.port, redis_service, nats_service
        )

        proc_a: CollectorProcess = await collector_factory(v2_tree, collector_id=COLLECTOR_ID)
        await start_task_instance(proc_a.grpc_target, task_id=DEVICE_ID, instance_id=DEVICE_ID)
        first = await wait_telemetry_value(redis_service)
        assert first["quality"] == "good"
        assert proc_a.terminate(timeout=30.0) == 0

        # ---- 同配置冷启动：不发任何 StartTask ----
        proc_b: CollectorProcess = await collector_factory(v2_tree, collector_id=COLLECTOR_ID)
        await wait_until(
            lambda: _task_running(proc_b), timeout=20.0, description="auto RUNNING on cold start"
        )
        later = await wait_telemetry_value(redis_service, after=str(first["timestamp"]))
        assert later["quality"] == "good"

    async def test_manual_stop_survives_restart_and_never_resurrects(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RT3-2：手动 STOP 持久化——重启后保持 STOPPED，数据静默，不被自动拉起。"""
        v2_tree = await _trees_and_sink(
            sink_service_factory, tmp_path, modbus_server.port, redis_service, nats_service
        )

        proc_a: CollectorProcess = await collector_factory(v2_tree, collector_id=COLLECTOR_ID)
        await start_task_instance(proc_a.grpc_target, task_id=DEVICE_ID, instance_id=DEVICE_ID)
        await wait_telemetry_value(redis_service)

        await stop_task(proc_a.grpc_target, DEVICE_ID)
        await wait_until(
            lambda: _task_stopped(proc_a), timeout=15.0, description="STOPPED after manual STOP"
        )
        # 停机后数据静默（覆盖若干采集周期）。
        settled = await wait_telemetry_value(redis_service)
        await asyncio.sleep(1.5)
        assert (await wait_telemetry_value(redis_service))["timestamp"] == settled["timestamp"]
        assert proc_a.terminate(timeout=30.0) == 0

        # ---- 同配置冷启动：持久 STOP 意图不得被解释成默认/待启动 ----
        proc_b: CollectorProcess = await collector_factory(v2_tree, collector_id=COLLECTOR_ID)
        await wait_until(
            lambda: _task_stopped(proc_b), timeout=20.0, description="STOPPED after cold start"
        )

        # 进程已稳定后再观察多个采集周期，确认无迟到复活与数据刷新。
        await asyncio.sleep(2.0)
        assert await _task_stopped(proc_b)
        latest = await wait_telemetry_value(redis_service)
        assert (
            latest["timestamp"] == settled["timestamp"]
        ), "manual STOP was resurrected across process restart"
