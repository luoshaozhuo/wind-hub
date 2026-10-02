"""协议 → Runtime → 真实外部 sink 的全链路集成测试（进程内）。

链路：真实 Modbus TCP server → Modbus 驱动 → AcquisitionEngine →
Sink 路由 → 真实 Kafka broker / 真实 PostgreSQL。验证侧是独立的
Kafka consumer / asyncpg 连接——数据必须真正穿过 broker / 数据库，
不看 Runtime 内部状态。

与 system 层（Phase 4）的差别：这里 Collector 是**进程内** assemble
的运行时；system 层才把 collector 作为独立子进程验收。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from tests.component.collector.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port
from tests.support.wait import wait_kafka_messages, wait_postgres_rows
from wind_hub_collector.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime

pytestmark = [pytest.mark.modbus, pytest.mark.kafka, pytest.mark.postgres]


async def _start_chain(config_dir: Path) -> AssembledRuntime:
    rt = assemble(config_dir)
    await start_runtime(rt)
    await rt.tasks.start_instance("modbus-telemetry:modbus-1")
    return rt


@pytest.fixture
async def modbus_server() -> AsyncIterator[ModbusMockServer]:
    server = ModbusMockServer(port=free_port())
    await server.start()
    yield server
    await server.stop()


class TestModbusToKafka:
    async def test_collected_points_reach_kafka(
        self, modbus_server: ModbusMockServer, kafka_service: str, tmp_path: Path
    ) -> None:
        topic = f"windhub-chain-{uuid.uuid4().hex[:12]}"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "kafka_sink",
                    "type": "kafka",
                    "params": {"bootstrap_servers": kafka_service, "topic": topic},
                }
            ],
            tasks=[
                {
                    "task_id": "modbus-telemetry",
                    "device": "modbus-1",
                    "point_group": "telemetry",
                    "interval": 0.2,
                    "targets": [{"sink": "kafka_sink"}],
                }
            ],
        )
        rt = await _start_chain(config_dir)
        try:
            messages = await wait_kafka_messages(
                kafka_service,
                topic,
                min_messages=4,
                timeout=30.0,
                match=lambda m: m.get("point_id") == "rotor.speed",
            )
            assert all(m["device_id"] == "modbus-1" for m in messages)
            assert all(m["value"] == pytest.approx(1200.5) for m in messages)
        finally:
            await stop_runtime(rt)


class TestModbusToPostgres:
    async def test_collected_points_reach_postgres(
        self, modbus_server: ModbusMockServer, postgres_service: str, tmp_path: Path
    ) -> None:
        table = f"windhub_chain_{uuid.uuid4().hex[:12]}"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=[
                {
                    "name": "db_sink",
                    "type": "db",
                    "params": {
                        "dsn": postgres_service,
                        "table": table,
                        "create_table": True,
                    },
                }
            ],
            tasks=[
                {
                    "task_id": "modbus-telemetry",
                    "device": "modbus-1",
                    "point_group": "telemetry",
                    "interval": 0.2,
                    "targets": [{"sink": "db_sink"}],
                }
            ],
        )
        rt = await _start_chain(config_dir)
        try:
            rows = await wait_postgres_rows(
                postgres_service,
                table,
                min_rows=4,
                timeout=30.0,
                where="point_id = 'rotor.speed'",
            )
            assert all(r["device_id"] == "modbus-1" for r in rows)
            assert all(json.loads(r["value"]) == pytest.approx(1200.5) for r in rows)
        finally:
            await stop_runtime(rt)
            conn = await asyncpg.connect(postgres_service)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                await conn.close()
