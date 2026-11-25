"""Single Storage instance Kafka -> TDengine capacity evidence."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaProducer

from tests.fixtures.services.r5_stack import R5StackError
from tests.system.e2e.eight_service.conftest import EightServiceHarness
from tests.system.e2e.eight_service.test_e2e_eight_service_smoke import _tdengine_row_count

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.capacity]


def test_storage_single_instance_capacity_60000_values_per_second(
    started: EightServiceHarness,
) -> None:
    harness = started
    token = uuid4().hex[:12]
    topic = "windhub.e2e.history"
    batches = int(os.environ.get("STORAGE_CAPACITY_BATCHES", "3600"))
    rate = float(os.environ.get("STORAGE_CAPACITY_BATCH_RATE", "120"))
    if batches <= 0 or rate <= 0:
        raise ValueError("positive capacity load required")
    points_per_batch = 500

    async def exercise() -> dict[str, object]:
        producer = AIOKafkaProducer(
            bootstrap_servers=harness.stack.kafka,
            acks="all",
            enable_idempotence=True,
            linger_ms=0,
            max_batch_size=524288,
        )
        await producer.start()
        published = 0
        started_at = 0.0
        try:
            started_at = time.monotonic()
            next_send = started_at
            pending: set[asyncio.Task[object]] = set()
            for batch_index in range(batches):
                builder = producer.create_batch()
                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                for point_index in range(points_per_batch):
                    sample_id = f"cap-{token}-{batch_index}-{point_index}"
                    payload = {
                        "schema_version": 2,
                        "data_level": "protocol",
                        "wind_farm_id": "e2e-final",
                        "collector_id": "capacity-collector",
                        "task_id": "capacity-task",
                        "device_type": "turbine",
                        "device_id": "WTG_SIM01",
                        "point_table_id": "point_table_modbus_wtg_sim_v1.2.0",
                        "batch_id": f"cap-{token}-{batch_index}",
                        "point_id": "active_power",
                        "business_point_id": None,
                        "sample_id": sample_id,
                        "value": float(point_index),
                        "unit": None,
                        "quality": "good",
                        "sample_time": now,
                        "publish_time": now,
                        "receive_time": now,
                        "max_staleness_ms": None,
                    }
                    assert (
                        builder.append(
                            key=b"e2e-final:capacity-collector:WTG_SIM01",
                            value=json.dumps(payload, separators=(",", ":")).encode(),
                            timestamp=None,
                        )
                        is not None
                    )
                builder.close()
                pending.add(asyncio.create_task(producer.send_batch(builder, topic, partition=0)))
                if len(pending) >= 32:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        confirmation = await task
                        await confirmation
                    published += len(done)
                next_send += 1 / rate
                await asyncio.sleep(max(0.0, next_send - time.monotonic()))
            if pending:
                confirmations = await asyncio.gather(*pending)
                await asyncio.gather(*confirmations)
                published += len(pending)
            publish_end = time.monotonic()
            diagnostics = harness.status("storage")
            print(f"storage status after publish: {diagnostics}")
            schema_deadline = time.monotonic() + 30
            while True:
                try:
                    physical = _tdengine_row_count(harness)
                    break
                except R5StackError:
                    if time.monotonic() >= schema_deadline:
                        raise
                    await asyncio.sleep(0.5)
            database, stable = str(physical["database"]), str(physical["stable"])
            expected = batches * points_per_batch
            deadline = time.monotonic() + 180
            last_count = 0
            while time.monotonic() < deadline:
                try:
                    rows = harness.stack.tdengine_sql(
                        f"SELECT COUNT(*) FROM `{database}`.`{stable}` "
                        f"WHERE sample_id LIKE 'cap-{token}-%'",
                        database=database,
                        timeout=30,
                    )["data"]
                except R5StackError:
                    await asyncio.sleep(1)
                    continue
                last_count = int(rows[0][0])
                if last_count >= expected:
                    break
                await asyncio.sleep(2)
            drained_at = time.monotonic()
            return {
                "target_values_per_second": rate * points_per_batch,
                "scheduled_batches": batches,
                "published_builder_batches": published,
                "published_values": published * points_per_batch,
                "publish_seconds": publish_end - started_at,
                "publish_batches_per_second": published / (publish_end - started_at),
                "tdengine_rows": last_count,
                "drain_seconds": drained_at - publish_end,
                "confirmed_values_per_second_end_to_end": last_count / (drained_at - started_at),
                "fully_persisted": last_count == expected,
            }
        finally:
            await producer.stop()

    result = asyncio.run(exercise())
    Path(os.environ.get("STORAGE_CAPACITY_RESULT", "/tmp/storage_capacity_result.json")).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    assert result["published_builder_batches"] == batches, result
    assert result["publish_batches_per_second"] >= rate * 0.95, result
    assert result["fully_persisted"], result
