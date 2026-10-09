"""Recovery：短周期故障/重载循环的资源稳定性——不得单调泄漏。

与 soak 的长时资源采样的区别：这里针对的是故障注入路径本身
（reconnect 循环、reload 循环）——这些路径涉及连接重建、协议实例
替换、acquisition handle 重启、sink consumer 重建，是句柄泄漏的
高发区。循环次数有限（10 次），断言的是「不单调增长」而非长时平稳。

观测点（全部在进程边界）：
- ``/proc/<pid>/fd`` 采样（含 socket/文件/管道）；
- ctl status 的 acquisitions 列表（Task Instance 唯一、无重复 handle）；
- GetMetricsSnapshot 的 reconnects 计数（确认故障循环真实发生）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import (
    ctl_instance_states,
    wait_status,
    write_modbus_file_config,
)
from tests.support.control import (
    apply_placement_and_start_instance,
    metrics_snapshot,
    reload_config,
)
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess
from tests.support.wait import read_jsonl, wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"

CYCLES = 10
#: FD 采样容忍的瞬时波动（日志 flush、metrics 连接等）；泄漏是单调增长，
#: 10 次循环后仍落在基线 +3 以内即不成立。
FD_SLACK = 3


async def _single_running_instance(proc: CollectorProcess) -> None:
    """断言本用例实例唯一且处于 RUNNING 生命周期状态。"""
    states = await ctl_instance_states(proc)
    assert states is not None
    assert states.get(INSTANCE_ID) == "running", f"unexpected instance states: {states}"


class TestReconnectFlapStability:
    async def test_ten_disconnect_cycles_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """RES-01：10 次断连/恢复循环后实例唯一、FD 不增长。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=2)

        baseline_reconnects = (
            await metrics_snapshot(proc.grpc_target)
        )["device_reconnects"].get("modbus-1", 0)
        baseline_fd = proc.fd_count()

        for cycle in range(CYCLES):
            await modbus_server.stop()
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 0,
                description=f"cycle {cycle}: disconnected",
            )
            await modbus_server.start()
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 1,
                description=f"cycle {cycle}: reconnected",
            )
            await _single_running_instance(proc)

        # 数据面在最后一次恢复后继续出数。
        settled = len(read_jsonl(sink_path))
        await wait_file_rows(sink_path, min_rows=settled + 2)

        reconnects = (
            await metrics_snapshot(proc.grpc_target)
        )["device_reconnects"].get("modbus-1", 0)
        assert reconnects - baseline_reconnects >= CYCLES, (
            f"expected >= {CYCLES} reconnects, got {reconnects - baseline_reconnects}"
        )
        final_fd = proc.fd_count()
        assert final_fd <= baseline_fd + FD_SLACK, (
            f"fd count grew from {baseline_fd} to {final_fd} over {CYCLES} flap cycles"
        )


class TestReloadStability:
    async def test_ten_reloads_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """RES-02：10 次 reload（task 重启 + sink 重建）后实例唯一、FD 不增长。

        每轮同时变更 task interval（触发 acquisition handle 重启）与 sink
        buffer_size（触发 sink 重建、consumer 协程替换）——两条重建路径
        都是 asyncio task / 句柄泄漏高发区。
        """
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=2)
        baseline_fd = proc.fd_count()

        for cycle in range(CYCLES):
            interval = 0.2 if cycle % 2 == 0 else 0.3
            buffer_size = 4 if cycle % 2 == 0 else 8

            def _mutate_tasks(data: dict, iv: float = interval) -> None:
                data["tasks"][0]["interval"] = iv

            def _mutate_sinks(data: dict, bs: int = buffer_size) -> None:
                data["sinks"][0]["connection"]["buffer_size"] = bs

            update_yaml(config_dir, "tasks.yaml", _mutate_tasks)
            update_yaml(config_dir, "sinks.yaml", _mutate_sinks)
            activated = await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id=f"rev-leak-{cycle}"
            )
            assert activated.success, f"cycle {cycle} activate failed: {list(activated.errors)}"
            await _single_running_instance(proc)

        # 采集在最后一轮配置下继续出数。
        settled = len(read_jsonl(sink_path))
        await wait_file_rows(sink_path, min_rows=settled + 2)
        await asyncio.sleep(1.0)  # 等最后一轮重建的旧句柄完成释放

        final_fd = proc.fd_count()
        assert final_fd <= baseline_fd + FD_SLACK, (
            f"fd count grew from {baseline_fd} to {final_fd} over {CYCLES} reloads"
        )
