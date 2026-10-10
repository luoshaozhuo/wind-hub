"""Recovery：多 Sink 故障隔离——一个外部 Sink 中断不得拖垮其余 Sink。

现场拓扑：单个采集 Task 同时扇出到两个 Sink（File + Redis）。已有
``test_sink_disconnect.py`` 验证的是「单 Sink 中断后能恢复」；本文件
验证的是中断**期间**的隔离性：健康 Sink 的数据流必须持续增长、采集
不停顿、进程不崩。Redis 中断用 Docker Compose 真实停服注入。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.services import compose
from tests.reliability.recovery.helpers import (
    telemetry_task,
    wait_status,
)
from tests.support.control import apply_placement_and_start_instance
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.redis_client import parse_address, point_key
from tests.support.wait import (
    read_csv,
    wait_file_rows,
    wait_redis_value,
)

pytestmark = pytest.mark.real_service

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"

#: 本文件 Redis sink 的 key_prefix（键规则见 tests.support.redis_client.point_key）。
REDIS_KEY_PREFIX = "wind-hub-iso"
ROTOR_KEY = point_key("modbus-1", "rotor.speed", prefix=REDIS_KEY_PREFIX)


def _multi_target_task(*sink_names: str) -> dict:
    task = telemetry_task(sink_names[0])
    task["targets"] = [{"sink": name} for name in sink_names]
    return task


async def _assert_file_flow_continues(sink_path: Path, rows_before: int) -> None:
    """故障窗口内 File sink 行数必须持续增长（隔离性的核心断言）。"""
    await wait_file_rows(sink_path, min_rows=rows_before + 4, timeout=20.0)


@pytest.mark.redis
class TestRedisOutageIsolation:
    async def test_file_sink_survives_redis_outage(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        """SINK-01：Redis 挂掉期间 File 持续增长；Redis 恢复后重新出数。"""
        host, port = parse_address(redis_service)
        sink_path = tmp_path / "out" / "telemetry.csv"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "file_sink",
                    "type": "file",
                    "connection": {
                        "path": str(sink_path),
                    },
                },
                {
                    "name": "redis_sink",
                    "type": "redis",
                    "connection": {
                        "host": host,
                        "port": port,
                        "key_prefix": REDIS_KEY_PREFIX,
                    },
                },
            ],
            tasks=[_multi_target_task("file_sink", "redis_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        service_stopped = False
        try:
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )
            baseline = await wait_redis_value(redis_service, ROTOR_KEY)
            await wait_file_rows(sink_path, min_rows=2)

            # ---- 故障：真实停止 Redis ----
            compose.compose_stop_service("redis")
            service_stopped = True

            # ---- 隔离：File 数据流不停，采集与进程不受影响 ----
            rows_before = len(read_csv(sink_path))
            await _assert_file_flow_continues(sink_path, rows_before)
            assert proc.is_running()
            await wait_status(proc, lambda p: p["running"] is True)

            # ---- 恢复：Redis 重新出数 ----
            compose.compose_start_service("redis")
            service_stopped = False
            await wait_redis_value(
                redis_service,
                ROTOR_KEY,
                timeout=60.0,
                match=lambda v: v["timestamp"] > baseline["timestamp"],
            )
        finally:
            if service_stopped:
                compose.compose_start_service("redis")
