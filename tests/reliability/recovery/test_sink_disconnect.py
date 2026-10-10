"""Recovery：外部 sink 服务中断全周期——Redis 停止 → 检测 → 进程存活 → 恢复。

故障注入手段是 Docker Compose 真实停止/启动服务（非 iptables 模拟）；
检测与恢复信号来自系统边界：独立 Redis 连接的点值 timestamp 推进、
ctl status 的 ``sinks_healthy``。用例在 finally 中恢复服务，保证
session 级 compose 栈不污染后续测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.services import compose
from tests.reliability.recovery.helpers import (
    RedisFlowObserver,
    telemetry_task,
    wait_status,
)
from tests.support.control import apply_placement_and_start_instance
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.redis_client import parse_address, point_key
from tests.support.wait import wait_redis_value

pytestmark = pytest.mark.real_service

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"

#: 本文件 Redis sink 的 key_prefix（键规则见 tests.support.redis_client.point_key）。
REDIS_KEY_PREFIX = "wind-hub-rec"
ROTOR_KEY = point_key("modbus-1", "rotor.speed", prefix=REDIS_KEY_PREFIX)


@pytest.mark.redis
class TestRedisOutageRecovery:
    async def test_service_outage_then_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        host, port = parse_address(redis_service)
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "redis_sink",
                    "type": "redis",
                    "connection": {
                        "host": host,
                        "port": port,
                        "key_prefix": REDIS_KEY_PREFIX,
                    },
                }
            ],
            tasks=[telemetry_task("redis_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        service_stopped = False
        observer = RedisFlowObserver(redis_service, ROTOR_KEY)
        try:
            # ---- 正常：点值持续写入 Redis（timestamp 随采集周期推进） ----
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )
            baseline = await wait_redis_value(redis_service, ROTOR_KEY)
            assert baseline["value"] == pytest.approx(1200.5)
            assert await observer.count_during(2.0) > 0

            # ---- 故障：真实停止 Redis ----
            compose.compose_stop_service("redis")
            service_stopped = True

            # ---- 检测：点值冻结，观测窗口内无任何更新；sink 健康翻转 ----
            await observer.wait_quiesced()
            assert await observer.count_during(5.0) == 0
            await wait_status(
                proc,
                lambda p: p["sinks_healthy"] == 0,
                timeout=60.0,
                description="sink detected unhealthy",
            )

            # ---- 主进程存活：Runtime 仍在运行 ----
            assert proc.is_running()
            await wait_status(proc, lambda p: p["running"] is True)

            # ---- 恢复：Redis 重启（compose_start_service 自带健康等待） ----
            compose.compose_start_service("redis")
            service_stopped = False

            # ---- 数据恢复：点值重新推进（故障期失败批次按丢弃语义不补） ----
            frozen_ts = baseline["timestamp"]
            recovered = await wait_redis_value(
                redis_service,
                ROTOR_KEY,
                timeout=60.0,
                match=lambda v: v["timestamp"] > frozen_ts,
            )
            assert recovered["value"] == pytest.approx(1200.5)

            # sink 健康在成功写入后恢复。
            await wait_status(
                proc,
                lambda p: p["sinks_healthy"] == 1,
                description="sink recovered healthy",
            )
        finally:
            if service_stopped:
                compose.compose_start_service("redis")
