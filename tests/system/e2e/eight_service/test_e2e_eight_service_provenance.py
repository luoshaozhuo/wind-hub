"""C5: real eight-service point-level reconciliation with durable Storage.

All services come from the shared CLI-managed E2E fixture. Kafka, TDengine,
NATS and Redis are real infrastructure. No direct service implementation is
substituted for the running process.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiokafka import AIOKafkaConsumer

from tests.system.e2e.eight_service.conftest import SERVICES, EightServiceHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.end_to_end,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]

DELIVERIES = (
    "e2e_kafka_protocol",
    "e2e_redis_power",
    "e2e_modbus_power",
    "e2e_gateway_power",
)
TOPIC = "windhub.e2e.history"
DEVICE = "WTG_SIM01"
POINT = "active_power"
TASK = "e2e_source_all"


def _items(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or {}
    items = data.get("samples") or data.get("items") or []
    assert isinstance(items, list), response
    return items


def _query(harness: EightServiceHarness, start: str, end: str) -> dict[str, Any]:
    return harness.cli(
        "storage",
        "samples",
        "e2e-final",
        start,
        end,
        "--level",
        "protocol",
        "--device-id",
        DEVICE,
        "--point-id",
        POINT,
        "--page-size",
        "1000",
        check=False,
        timeout=60,
    )


def _truth_points(harness: EightServiceHarness) -> list[dict[str, Any]]:
    """Independent source recordings, never reconstructed from Kafka data."""
    recording_root = harness.root / "artifacts" / "e2e-final" / "data-generator"
    points: list[dict[str, Any]] = []
    for path in sorted(recording_root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            for point in record["points"]:
                if point["device_id"] == DEVICE and point["point_id"] == POINT:
                    points.append(point)
    return points


def _source_truth_matches(
    harness: EightServiceHarness, sample_time: str, value: float
) -> bool:
    sampled_at = datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
    candidates = [
        (sampled_at - datetime.fromisoformat(point["source_time"])).total_seconds()
        for point in _truth_points(harness)
        if isinstance(point["value"], (int, float))
        and abs(float(point["value"]) - value) <= max(1e-3, abs(value) * 2e-3)
    ]
    return any(0 <= lag <= 2.0 for lag in candidates)


def test_eight_service_provenance_and_restart_readback(
    started: EightServiceHarness,
) -> None:
    harness = started
    assert all(harness.status(name)["status"] == "SUCCESS" for name in SERVICES)

    available = harness.cli("sink", "deliveries")
    assert available["status"] == "SUCCESS", available
    delivery_ids = {
        item.get("delivery_id") or item.get("id")
        for item in (available.get("data") or {}).get("deliveries", [])
    }
    assert set(DELIVERIES) <= delivery_ids, delivery_ids
    for delivery_id in DELIVERIES:
        result = harness.cli("sink", "start", delivery_id, "--yes", timeout=90)
        assert result["status"] == "SUCCESS", (delivery_id, result)

    started_at = datetime.now(UTC)
    deadline_at = started_at + timedelta(minutes=3)
    start = (started_at - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = deadline_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _exercise() -> dict[str, Any]:
        # Subscribe before the generator starts; latest offsets exclude stale
        # records from other test runs and avoid an arrival race.
        consumer = AIOKafkaConsumer(
            TOPIC,
            bootstrap_servers=harness.stack.kafka,
            group_id=None,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            # Force Kafka partition assignment before starting the source.
            await consumer.getmany(timeout_ms=100)
            created = harness.cli(
                "data-generator",
                "start",
                "e2e-plant-physical",
                "--yes",
                timeout=90,
            )
            assert created["status"] == "SUCCESS", created
            deadline = asyncio.get_running_loop().time() + 120
            while asyncio.get_running_loop().time() < deadline:
                messages = await consumer.getmany(timeout_ms=1500, max_records=500)
                for records in messages.values():
                    for record in records:
                        row = json.loads(record.value)
                        if (
                            row.get("data_level") == "protocol"
                            and row.get("wind_farm_id") == "e2e-final"
                            and row.get("collector_id") == "collector-e2e-reliable"
                            and row.get("device_id") == DEVICE
                            and row.get("point_id") == POINT
                        ):
                            return row
            raise AssertionError("no live Kafka point from the running reliable Collector")
        finally:
            await consumer.stop()

    wire = asyncio.run(_exercise())
    assert wire["task_id"] == TASK, "task identity must come from Task Instance"
    assert wire["sample_id"] and wire["batch_id"] and wire["point_table_id"]
    assert wire["quality"] == "good"
    assert type(wire["value"]) in (int, float)
    truth_deadline = time.monotonic() + 30
    while not _source_truth_matches(harness, wire["sample_time"], float(wire["value"])):
        if time.monotonic() >= truth_deadline:
            raise AssertionError(
                "Kafka point does not match any time-aligned Generator JSONL source: "
                f"{wire}"
            )
        time.sleep(0.5)

    # Query Storage by the same physical device/point; never accept any other
    # point as proof. Match exact sample_id and the value at this timestamp.
    deadline = time.monotonic() + 120
    last: dict[str, Any] = {}
    found: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _query(harness, start, end)
        if last.get("status") == "SUCCESS":
            found = next(
                (
                    row for row in _items(last)
                    if row.get("sample_id") == wire["sample_id"]
                    and row.get("point_id") == POINT
                    and row.get("device_id") == DEVICE
                ),
                None,
            )
            if found is not None:
                break
        time.sleep(1)
    assert found is not None, f"Kafka sample absent from Storage: {wire}; {last}"
    assert float(found["value_f64"]) == pytest.approx(
        float(wire["value"]), rel=1e-6, abs=1e-5
    )

    # A process restart cannot erase an already persisted sample. The same
    # source identity and value must be independently queryable afterward.
    restarted = harness.cli("service", "restart", "storage", "--yes", timeout=120)
    assert restarted["status"] == "SUCCESS", restarted
    harness.wait_targets("storage")
    replay = _query(harness, start, end)
    assert replay["status"] == "SUCCESS", replay
    same = next(
        (row for row in _items(replay) if row.get("sample_id") == wire["sample_id"]),
        None,
    )
    assert same is not None, f"confirmed sample vanished after restart: {wire['sample_id']}"
    assert same["content_sha256"] == found["content_sha256"]
    assert same["logical_key"] == found["logical_key"]
