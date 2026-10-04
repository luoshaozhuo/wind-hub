"""System E2E：Modbus → Collector 子进程 → 真实 sink 的数据面验收。

链路：真实 Modbus TCP 从站 → Collector subprocess（console script 启动）
→ 真实 File/Kafka/PostgreSQL sink。任务启停经 CollectorControlService RPC
下发（与 Server → Collector 生产控制路径一致）；验证全部发生在系统
边界——读输出文件、独立 Kafka consumer、独立 SQL 连接，不读取
Collector 进程内部状态。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.component.collector.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import apply_placement_and_start_instance, stop_instance
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


def _task(sink_name: str) -> dict:
    return {
        "task_id": "modbus-telemetry",
        "device": "modbus-1",
        "point_group": "telemetry",
        "interval": 0.2,
        "targets": [{"sink": sink_name}],
    }


def _file_config(base: Path, port: int, sink_path: Path) -> Path:
    """单设备 + File sink 的现场配置（小缓冲/短间隔，保证及时落盘）。"""
    return write_functional_config(
        base,
        port,
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {
                    "path": str(sink_path),
                    "buffer_size": 4,
                    "flush_interval": 0.5,
                },
            }
        ],
        tasks=[_task("file_sink")],
    )


class TestModbusToFile:
    async def test_started_task_streams_points_to_file(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = _file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)

        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )

        rows = await wait_file_rows(
            sink_path,
            min_rows=4,
            match=lambda r: r["point_id"] == "rotor.speed",
        )
        assert all(r["device_id"] == "modbus-1" for r in rows)
        assert all(r["value"] == pytest.approx(1200.5) for r in rows)
        assert all(r["quality"] == "good" for r in rows)
        # 同一任务周期应同时产出组内其余点位。
        point_ids = {r["point_id"] for r in read_jsonl(sink_path)}
        assert {"rotor.speed", "gen.power", "temp.int", "setpoint.power"} <= point_ids

    async def test_stopped_task_halts_delivery(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = _file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)

        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=4)
        await stop_instance(proc.grpc_target, INSTANCE_ID)

        # 静默验证（不是就绪等待）：先等残余缓冲落盘，再确认行数在
        # 数个采集周期内不再增长——停止后不允许有新采集写入。
        await asyncio.sleep(1.0)
        settled = len(read_jsonl(sink_path))
        await asyncio.sleep(0.8)
        assert len(read_jsonl(sink_path)) == settled


@pytest.mark.kafka
class TestModbusToKafka:
    async def test_collected_points_reach_broker(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        kafka_service: str,
        tmp_path: Path,
    ) -> None:
        topic = f"windhub-sys-{uuid.uuid4().hex[:12]}"
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
            tasks=[_task("kafka_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)

        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )

        messages = await wait_kafka_messages(
            kafka_service,
            topic,
            min_messages=4,
            match=lambda m: m.get("point_id") == "rotor.speed",
        )
        assert all(m["device_id"] == "modbus-1" for m in messages)
        assert all(m["value"] == pytest.approx(1200.5) for m in messages)


@pytest.mark.postgres
class TestModbusToPostgres:
    async def test_collected_points_reach_database(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        postgres_service: str,
        tmp_path: Path,
    ) -> None:
        table = f"windhub_sys_{uuid.uuid4().hex[:12]}"
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
            tasks=[_task("db_sink")],
        )
        proc: CollectorProcess = await collector_factory(config_dir)
        try:
            await apply_placement_and_start_instance(
                proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
            )

            rows = await wait_postgres_rows(
                postgres_service,
                table,
                min_rows=4,
                where="point_id = 'rotor.speed'",
            )
            assert all(r["device_id"] == "modbus-1" for r in rows)
            # asyncpg 把 jsonb 返回为 JSON 文本——解码后比较。
            assert all(json.loads(r["value"]) == pytest.approx(1200.5) for r in rows)
        finally:
            conn = await asyncpg.connect(postgres_service)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                await conn.close()
