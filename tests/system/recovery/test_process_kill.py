"""Fault injection：进程强杀（SIGKILL）后的服务恢复。

与 recovery/test_restart_recovery.py 的优雅停机不同，SIGKILL 不给
finally/atexit 任何机会：连接半截、Redis 写入中断、锁不释放。系统边界
要求：同配置重启后控制面与数据面完整恢复——不依赖被杀进程的任何
清理动作。观测在 Redis 最新值边界进行（与其余 recovery 用例一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance
from tests.support.process import CollectorProcess, run_cli_async
from tests.support.recovery_helpers import (
    TASK_ID,
    wait_telemetry_value,
    write_modbus_redis_config,
)

pytestmark = [
    pytest.mark.reliability,
    pytest.mark.modbus,
    pytest.mark.redis,
    pytest.mark.real_service,
]

INSTANCE_ID = f"{TASK_ID}:modbus-1"
WORKER_ID = "kill-test"


class TestProcessKillRecovery:
    async def test_sigkill_then_restart_restores_service(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        # 消费侧独立存活：Collector 被 SIGKILL 期间 sink_service 不动，
        # 重启后数据面经同一 NATS 总线恢复。
        await sink_service_factory(config_dir, deployment_id="kill-test-sink")

        # ---- 第一个实例：正常采集后直接 SIGKILL（无优雅停机） ----
        proc_a: CollectorProcess = await collector_factory(config_dir, collector_id=WORKER_ID)
        await start_task_instance(
            proc_a.grpc_target,
            task_id=TASK_ID,
            instance_id=INSTANCE_ID,
        )
        first = await wait_telemetry_value(redis_service)
        proc_a.kill_tree()
        assert not proc_a.is_running()

        # ---- 同配置重启：控制面恢复、采集可重新启动、数据继续到达 ----
        proc_b: CollectorProcess = await collector_factory(config_dir, collector_id=WORKER_ID)
        info = await run_cli_async("info", target=proc_b.grpc_target)
        assert info.returncode == 0
        assert json.loads(info.stdout)["collector_id"] == WORKER_ID

        baseline = str(first["timestamp"])
        await start_task_instance(
            proc_b.grpc_target,
            task_id=TASK_ID,
            instance_id=INSTANCE_ID,
        )
        await wait_telemetry_value(redis_service, after=baseline)
