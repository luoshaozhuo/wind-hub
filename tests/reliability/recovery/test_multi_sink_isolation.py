"""Recovery：多 Sink 故障隔离——一个外部 Sink 中断不得拖垮其余 Sink。

现场拓扑：单个采集 Task 同时扇出到两个 Sink（File + Kafka / File +
PostgreSQL）。已有 ``test_sink_disconnect.py`` 验证的是「单 Sink 中断后
能恢复」；本文件验证的是中断**期间**的隔离性：健康 Sink 的数据流必须
持续增长、采集不停顿、进程不崩。Kafka/PostgreSQL 中断用 Docker Compose
真实停服注入。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.component.collector.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.services import compose
from tests.reliability.recovery.helpers import telemetry_task, wait_status
from tests.support.control import apply_placement_and_start_instance
from tests.support.process import CollectorProcess
from tests.support.wait import (
    read_jsonl,
    wait_file_rows,
    wait_kafka_messages,
    wait_postgres_rows,
)

pytestmark = pytest.mark.real_service

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"


def _multi_target_task(*sink_names: str) -> dict:
    task = telemetry_task(sink_names[0])
    task["targets"] = [{"sink": name} for name in sink_names]
    return task


async def _assert_file_flow_continues(sink_path: Path, rows_before: int) -> None:
    """故障窗口内 File sink 行数必须持续增长（隔离性的核心断言）。"""
    await wait_file_rows(sink_path, min_rows=rows_before + 4, timeout=20.0)


@pytest.mark.kafka
class TestKafkaOutageIsolation:
    async def test_file_sink_survives_kafka_outage(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        kafka_service: str,
        tmp_path: Path,
    ) -> None:
        """SINK-01：Kafka 挂掉期间 File 持续增长；Kafka 恢复后重新出数。"""
        topic = f"windhub-iso-{uuid.uuid4().hex[:12]}"
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "file_sink",
                    "type": "file",
                    "connection": {
                        "path": str(sink_path),
                        "buffer_size": 4,
                        "flush_interval": 0.5,
                    },
                },
                {
                    "name": "kafka_sink",
                    "type": "kafka",
                    "connection": {"bootstrap_servers": kafka_service, "topic": topic},
                },
            ],
            tasks=[_multi_target_task("file_sink", "kafka_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        broker_stopped = False
        try:
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )
            await wait_kafka_messages(kafka_service, topic, min_messages=2)
            await wait_file_rows(sink_path, min_rows=2)

            # ---- 故障：真实停止 broker ----
            compose.compose_stop_service("kafka")
            broker_stopped = True

            # ---- 隔离：File 数据流不停，采集与进程不受影响 ----
            rows_before = len(read_jsonl(sink_path))
            await _assert_file_flow_continues(sink_path, rows_before)
            assert proc.is_running()
            await wait_status(proc, lambda p: p["running"] is True)

            # ---- 恢复：Kafka 重新出数 ----
            compose.compose_start_service("kafka")
            broker_stopped = False
            await wait_kafka_messages(kafka_service, topic, min_messages=1)
        finally:
            if broker_stopped:
                compose.compose_start_service("kafka")


@pytest.mark.postgres
class TestPostgresOutageIsolation:
    async def test_file_sink_survives_postgres_outage(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        postgres_service: str,
        tmp_path: Path,
    ) -> None:
        """SINK-02：PostgreSQL 挂掉期间 File 持续增长；DB 恢复后重新落库。"""
        table = f"windhub_iso_{uuid.uuid4().hex[:12]}"
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "file_sink",
                    "type": "file",
                    "connection": {
                        "path": str(sink_path),
                        "buffer_size": 4,
                        "flush_interval": 0.5,
                    },
                },
                {
                    "name": "db_sink",
                    "type": "db",
                    "connection": {
                        "dsn": postgres_service,
                        "table": table,
                        "create_table": True,
                    },
                },
            ],
            tasks=[_multi_target_task("file_sink", "db_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        db_stopped = False
        try:
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )
            await wait_postgres_rows(postgres_service, table, min_rows=2)
            await wait_file_rows(sink_path, min_rows=2)

            # ---- 故障：真实停止数据库 ----
            compose.compose_stop_service("postgres")
            db_stopped = True

            # ---- 隔离：File 数据流不停，采集与进程不受影响 ----
            rows_before = len(read_jsonl(sink_path))
            await _assert_file_flow_continues(sink_path, rows_before)
            assert proc.is_running()
            await wait_status(proc, lambda p: p["running"] is True)

            # ---- 恢复：数据库重新落库 ----
            compose.compose_start_service("postgres")
            db_stopped = False
            await wait_postgres_rows(
                postgres_service,
                table,
                min_rows=1,
                where=f"\"timestamp\" > NOW() - INTERVAL '60 seconds'",
            )
        finally:
            if db_stopped:
                compose.compose_start_service("postgres")
