"""Fault injection：进程强杀（SIGKILL）后的服务恢复。

与 recovery/test_restart_recovery.py 的优雅停机不同，SIGKILL 不给
finally/atexit 任何机会：缓冲数据丢失、连接半截、锁不释放。系统边界
要求：同配置重启后控制面与数据面完整恢复——不依赖被杀进程的任何
清理动作。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import write_modbus_file_config
from tests.support.control import apply_placement_and_start_instance
from tests.support.process import CollectorProcess, run_ctl_async
from tests.support.wait import read_jsonl, wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"
WORKER_ID = "kill-test"


class TestProcessKillRecovery:
    async def test_sigkill_then_restart_restores_service(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.csv"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)

        # ---- 第一个实例：正常采集后直接 SIGKILL（无优雅停机） ----
        proc_a: CollectorProcess = await collector_factory(
            config_dir, collector_id=WORKER_ID
        )
        await apply_placement_and_start_instance(
            proc_a.grpc_target,
            task_id=TASK_ID,
            instance_id=INSTANCE_ID,
            worker_id=WORKER_ID,
        )
        await wait_file_rows(sink_path, min_rows=3)
        proc_a.kill_tree()
        assert not proc_a.is_running()

        # ---- 同配置重启：控制面恢复、采集可重新启动、数据继续到达 ----
        proc_b: CollectorProcess = await collector_factory(
            config_dir, collector_id=WORKER_ID
        )
        info = await run_ctl_async("info", target=proc_b.grpc_target)
        assert info.returncode == 0
        assert json.loads(info.stdout)["collector_id"] == WORKER_ID

        baseline = len(read_jsonl(sink_path))
        await apply_placement_and_start_instance(
            proc_b.grpc_target,
            task_id=TASK_ID,
            instance_id=INSTANCE_ID,
            worker_id=WORKER_ID,
        )
        await wait_file_rows(sink_path, min_rows=baseline + 2)

    async def test_sigkill_during_active_collection_loses_at_most_buffer(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """采集中途 SIGKILL：已落盘数据不得损坏（JSONL 每行可解析）。"""
        sink_path = tmp_path / "out" / "telemetry.csv"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(
            config_dir, collector_id=WORKER_ID
        )
        await apply_placement_and_start_instance(
            proc.grpc_target,
            task_id=TASK_ID,
            instance_id=INSTANCE_ID,
            worker_id=WORKER_ID,
        )
        await wait_file_rows(sink_path, min_rows=4)
        proc.kill_tree()

        # 落盘文件必须保持可解析——不允许半截行破坏后续消费。
        rows = read_jsonl(sink_path)
        assert rows, "rows flushed before kill must survive"
        assert all(r["device_id"] == "modbus-1" for r in rows)
