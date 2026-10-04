"""Recovery：外部 sink 服务中断全周期——broker/数据库停止 → 检测 → 进程存活 → 恢复。

故障注入手段是 Docker Compose 真实停止/启动服务（非 iptables 模拟）；
检测与恢复信号来自系统边界：独立 consumer 的新消息计数、SQL 行数、
ctl status 的 ``sinks_healthy``。每个用例在 finally 中恢复服务，保证
session 级 compose 栈不污染后续测试。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.component.collector.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.services import compose
from tests.reliability.recovery.helpers import (
    KafkaFlowObserver,
    telemetry_task,
    wait_status,
)
from tests.support.control import apply_placement_and_start_instance
from tests.support.process import CollectorProcess
from tests.support.wait import wait_kafka_messages, wait_postgres_rows

pytestmark = pytest.mark.real_service

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"


@pytest.mark.kafka
class TestKafkaOutageRecovery:
    async def test_broker_outage_then_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        kafka_service: str,
        tmp_path: Path,
    ) -> None:
        topic = f"windhub-rec-{uuid.uuid4().hex[:12]}"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "kafka_sink",
                    "type": "kafka",
                    "connection": {"bootstrap_servers": kafka_service, "topic": topic},
                }
            ],
            tasks=[telemetry_task("kafka_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        broker_stopped = False
        # 观测器必须在故障注入前建立连接：broker 停止后新 consumer 无法
        # bootstrap，事中新建 consumer 的检测路径本身就会失败。
        observer = KafkaFlowObserver(kafka_service, topic)
        await observer.start()
        try:
            # ---- 正常：消息到达 broker ----
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )
            baseline = await wait_kafka_messages(kafka_service, topic, min_messages=3)

            # ---- 故障：真实停止 broker ----
            compose.compose_stop_service("kafka")
            broker_stopped = True

            # ---- 检测：排空存量后，观测窗口内无任何新消息到达 ----
            await observer.drain()
            assert await observer.count_during(5.0) == 0

            # ---- 主进程存活：Runtime 仍在运行 ----
            assert proc.is_running()
            await wait_status(proc, lambda p: p["running"] is True)

            # ---- 恢复：broker 重启（compose_start_service 自带健康等待） ----
            compose.compose_start_service("kafka")
            broker_stopped = False

            # ---- 数据恢复：新消息（含故障期积压）重新到达 ----
            messages = await wait_kafka_messages(
                kafka_service,
                topic,
                min_messages=len(baseline) + 2,
                timeout=60.0,
            )
            rotor = [m for m in messages if m.get("point_id") == "rotor.speed"]
            assert rotor, "rotor.speed messages missing after broker recovery"
            assert all(m["value"] == pytest.approx(1200.5) for m in rotor)
        finally:
            # 先恢复 broker 再关观测器：consumer.stop() 需要与 coordinator
            # 通信，broker 死亡时关闭会以 CancelledError 收场。
            if broker_stopped:
                compose.compose_start_service("kafka")
            await observer.close()


@pytest.mark.postgres
class TestPostgresOutageRecovery:
    async def test_database_outage_then_recovery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        postgres_service: str,
        tmp_path: Path,
    ) -> None:
        table = f"windhub_rec_{uuid.uuid4().hex[:12]}"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "db_sink",
                    "type": "db",
                    "connection": {
                        "dsn": postgres_service,
                        "table": table,
                        "create_table": True,
                    },
                }
            ],
            tasks=[telemetry_task("db_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        db_stopped = False
        try:
            # ---- 正常：行落库 ----
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )
            baseline = await wait_postgres_rows(postgres_service, table, min_rows=3)

            # ---- 故障：真实停止数据库 ----
            compose.compose_stop_service("postgres")
            db_stopped = True

            # ---- 检测：asyncpg 写立即失败，sink 健康在边界可见地翻转 ----
            await wait_status(
                proc,
                lambda p: p["sinks_healthy"] == 0,
                timeout=60.0,
                description="sink detected unhealthy",
            )

            # ---- 主进程存活 ----
            assert proc.is_running()
            await wait_status(proc, lambda p: p["running"] is True)

            # ---- 恢复：数据库重启，连接池重建 ----
            compose.compose_start_service("postgres")
            db_stopped = False

            # ---- 数据恢复：新行继续落库（故障期失败批次按丢弃语义不补） ----
            rows = await wait_postgres_rows(
                postgres_service,
                table,
                min_rows=len(baseline) + 2,
                timeout=60.0,
            )
            assert all(r["device_id"] == "modbus-1" for r in rows)

            # sink 健康在成功写入后恢复。
            await wait_status(
                proc,
                lambda p: p["sinks_healthy"] == 1,
                description="sink recovered healthy",
            )
        finally:
            if db_stopped:
                compose.compose_start_service("postgres")
            import asyncpg

            conn = await asyncpg.connect(postgres_service)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                await conn.close()
