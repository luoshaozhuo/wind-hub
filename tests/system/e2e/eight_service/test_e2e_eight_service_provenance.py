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

import grpc
import pytest
from contracts import data_generator_pb2 as generator_pb
from contracts import data_generator_pb2_grpc as generator_rpc
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

    async def _exercise() -> list[dict[str, Any]]:
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
            selected: dict[str, dict[str, Any]] = {}
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
                            selected[row["sample_id"]] = row
                            if len(selected) >= 5:
                                return list(selected.values())
            raise AssertionError(
                f"expected five distinct live Kafka samples, got {len(selected)}"
            )
        finally:
            await consumer.stop()

    wire_records = asyncio.run(_exercise())
    assert len(wire_records) == 5
    source_ids = {row["sample_id"] for row in wire_records}
    assert len(source_ids) == 5
    for wire in wire_records:
        assert wire["task_id"] == TASK, "task identity must come from Task Instance"
        assert wire["sample_id"] and wire["batch_id"] and wire["point_table_id"]
        assert wire["quality"] == "good"
        assert type(wire["value"]) in (int, float)

    truth_deadline = time.monotonic() + 30
    while not all(
        _source_truth_matches(harness, row["sample_time"], float(row["value"]))
        for row in wire_records
    ):
        if time.monotonic() >= truth_deadline:
            raise AssertionError(
                "Kafka data not time/value-aligned with independent Generator truth"
            )
        time.sleep(0.5)

    # Exactly the five independently observed source identities must exist in
    # Storage with matching scalar values. No count-based substitution.
    deadline = time.monotonic() + 120
    last: dict[str, Any] = {}
    persisted: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        last = _query(harness, start, end)
        if last.get("status") == "SUCCESS":
            persisted = {
                row["sample_id"]: row for row in _items(last)
                if row.get("sample_id") in source_ids
                and row.get("point_id") == POINT
                and row.get("device_id") == DEVICE
            }
            if len(persisted) == len(source_ids):
                break
        time.sleep(1)
    assert set(persisted) == source_ids, {
        "missing_count": len(source_ids - set(persisted)),
        "missing_ids": sorted(source_ids - set(persisted)),
        "last_query": last,
    }
    for wire in wire_records:
        stored = persisted[wire["sample_id"]]
        assert float(stored["value_f64"]) == pytest.approx(
            float(wire["value"]), rel=1e-6, abs=1e-5
        )
        assert stored["quality"] == wire["quality"]
        assert stored["logical_key"] and stored["content_sha256"]

    # Restart Storage using the official CLI, then reconcile the same five
    # identities and content hashes. No lost or silently rewritten rows.
    restarted = harness.cli("service", "restart", "storage", "--yes", timeout=120)
    assert restarted["status"] == "SUCCESS", restarted
    harness.wait_targets("storage")
    replay = _query(harness, start, end)
    assert replay["status"] == "SUCCESS", replay
    recovered = {
        row["sample_id"]: row for row in _items(replay)
        if row.get("sample_id") in source_ids
    }
    assert set(recovered) == source_ids
    for sample_id in source_ids:
        assert recovered[sample_id]["content_sha256"] == persisted[sample_id]["content_sha256"]
        assert recovered[sample_id]["logical_key"] == persisted[sample_id]["logical_key"]


@pytest.mark.parametrize(
    ("point_id", "requested"),
    [
        ("agc_power_setpoint", "1500.0"),
        ("avc_voltage_setpoint", "1.02"),
    ],
)
def test_eight_service_commander_gateway_generator_applied(
    started: EightServiceHarness, point_id: str, requested: str
) -> None:
    """Only Generator's request-scoped applied step can confirm control."""
    harness = started
    generation = harness.cli(
        "data-generator", "start", "e2e-plant-physical", "--yes", timeout=90
    )
    assert generation["status"] == "SUCCESS", generation
    generation_id = str(generation["data"]["generation_id"])
    try:
        dispatched = harness.cli(
            "commander",
            "write",
            "PPC_SIM01",
            point_id,
            requested,
            "--yes",
            timeout=90,
        )
        assert dispatched["status"] == "SUCCESS", dispatched
        command = dispatched["data"]
        assert command["accepted"] is True, command
        command_id = str(command["command_id"])

        deadline = time.monotonic() + 60
        settled: dict[str, Any] = {}
        while time.monotonic() < deadline:
            reply = harness.cli(
                "commander", "result", command_id, check=False, timeout=30
            )
            settled = reply.get("data") or {}
            if settled.get("state") in ("SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"):
                break
            time.sleep(0.5)
        assert settled.get("state") == "SUCCEEDED", settled
        assert settled.get("outcome") == "CONFIRMED_SUCCESS", settled
        assert settled.get("io_settled") is True, settled

        # Independent authenticated caller directly queries the Generator's
        # durable input-execution evidence. This is not Commander self-report.
        token = harness.env["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
        with grpc.insecure_channel("127.0.0.1:52061") as channel:
            execution = generator_rpc.DataGeneratorServiceStub(
                channel
            ).GetInputExecution(
                generator_pb.GetInputExecutionRequest(
                    generation_id=generation_id, request_id=command_id
                ),
                timeout=10,
                metadata=(("authorization", f"Bearer {token}"),),
            )
        assert execution.request_id == command_id
        assert execution.state == "APPLIED"
        assert execution.effective_step > 0
    finally:
        harness.cli(
            "data-generator",
            "stop",
            generation_id,
            "--yes",
            check=False,
            timeout=60,
        )
