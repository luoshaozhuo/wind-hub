"""C5: real eight-service point-level reconciliation with durable Storage.

All services come from the shared CLI-managed E2E fixture. Kafka, TDengine,
NATS and Redis are real infrastructure. No direct service implementation is
substituted for the running process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from aiokafka import AIOKafkaConsumer

from contracts import data_generator_pb2 as generator_pb
from contracts import data_generator_pb2_grpc as generator_rpc
from contracts import industrial_gateway_pb2 as gateway_pb
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from storage_service.domain.quality_bitmap import unpack_quality_bitmap
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


def _query(harness: EightServiceHarness, start: str, end: str) -> list[dict[str, Any]]:
    """读取全部游标页面，并保留每个物理 Schema 的列与质量位图。"""
    groups: list[dict[str, Any]] = []
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(1000):
        arguments = [
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
        ]
        if page_token:
            arguments.extend(("--page-token", page_token))
        response = harness.cli(*arguments, check=False, timeout=60)
        assert response.get("status") == "SUCCESS", response
        data = response.get("data") or {}
        page_groups = data.get("groups")
        assert isinstance(page_groups, list), response
        groups.extend(page_groups)
        next_token = data.get("next_page_token") or ""
        complete = data.get("complete")
        assert isinstance(complete, bool), response
        if not next_token:
            assert complete, response
            return groups
        assert not complete and next_token not in seen_tokens, response
        seen_tokens.add(next_token)
        page_token = next_token
    raise AssertionError("Storage grouped query exceeded 1000 pages")


def _time_us(text: str) -> int:
    sampled = datetime.fromisoformat(text.replace("Z", "+00:00"))
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = sampled.astimezone(UTC) - epoch
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _wire_point(record: dict[str, Any]) -> tuple[float, int]:
    assert record.get("schema_version") == 3, record
    points = record.get("points")
    assert isinstance(points, dict) and POINT in points, record
    entry = points[POINT]
    assert isinstance(entry, list) and len(entry) == 2, record
    value, quality = entry
    assert type(value) in (int, float) and type(quality) is int, record
    return float(value), quality


def _stored_points(groups: list[dict[str, Any]]) -> dict[int, tuple[float, int]]:
    """以完整设备行的 sample_time 定位，并核对列对齐的值与两位质量。"""
    result: dict[int, tuple[float, int]] = {}
    for group in groups:
        assert group.get("device_id") == DEVICE, group
        columns = group.get("columns") or []
        point_indexes = [
            index for index, column in enumerate(columns) if column.get("point_id") == POINT
        ]
        assert len(point_indexes) == 1, group
        point_index = point_indexes[0]
        for row in group.get("rows") or []:
            values = row.get("values") or []
            assert len(values) == len(columns), (group, row)
            quality_bitmap = base64.b64decode(row["quality"], validate=True)
            qualities = unpack_quality_bitmap(quality_bitmap, len(columns))
            stored_value = values[point_index]
            assert set(stored_value) == {"value_f64"}, stored_value
            identity = (
                float(stored_value["value_f64"]),
                int(qualities[point_index]),
            )
            timestamp = _time_us(row["sample_time"])
            previous = result.setdefault(timestamp, identity)
            assert previous == identity, (timestamp, previous, identity)
    return result


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


def _source_truth_matches(harness: EightServiceHarness, sample_time: str, value: float) -> bool:
    sampled_at = datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
    candidates = [
        (sampled_at - datetime.fromisoformat(point["source_time"])).total_seconds()
        for point in _truth_points(harness)
        if isinstance(point["value"], int | float)
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
            # Wait for a concrete latest-offset assignment and drain all
            # pre-existing records before starting the source.
            assignment_deadline = asyncio.get_running_loop().time() + 15
            while not consumer.assignment():
                await consumer.getmany(timeout_ms=250)
                if asyncio.get_running_loop().time() >= assignment_deadline:
                    raise AssertionError("Kafka consumer never received partition assignment")
            drain_deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < drain_deadline:
                backlog = await consumer.getmany(timeout_ms=250, max_records=5000)
                if not backlog:
                    break
            created = harness.cli(
                "data-generator",
                "start",
                "e2e-plant-physical",
                "--yes",
                timeout=90,
            )
            assert created["status"] == "SUCCESS", created
            deadline = asyncio.get_running_loop().time() + 120
            # Collector may sample the Gateway's initial zero-valued store
            # before the first physical step is delivered. Select only samples
            # at or after the independent source watermark.
            truth_start: datetime | None = None
            selected: dict[int, dict[str, Any]] = {}
            while asyncio.get_running_loop().time() < deadline:
                if truth_start is None:
                    points = _truth_points(harness)
                    if points:
                        truth_start = min(
                            datetime.fromisoformat(point["source_time"]) for point in points
                        )
                messages = await consumer.getmany(timeout_ms=1500, max_records=500)
                for records in messages.values():
                    for record in records:
                        row = json.loads(record.value)
                        if (
                            row.get("schema_version") != 3
                            or row.get("data_level") != "protocol"
                            or row.get("wind_farm_id") != "e2e-final"
                            or row.get("device_id") != DEVICE
                        ):
                            continue
                        sampled_at = datetime.fromisoformat(
                            row["sample_time"].replace("Z", "+00:00")
                        )
                        points = row.get("points")
                        if (
                            truth_start is not None
                            and sampled_at >= truth_start
                            and isinstance(points, dict)
                            and POINT in points
                        ):
                            _wire_point(row)
                            selected[_time_us(row["sample_time"])] = row
                            if len(selected) >= 5:
                                return list(selected.values())
            raise AssertionError(
                f"expected five distinct live Kafka device rows, got {len(selected)}"
            )
        finally:
            await consumer.stop()

    wire_records = asyncio.run(_exercise())
    assert len(wire_records) == 5
    source_times = {_time_us(row["sample_time"]) for row in wire_records}
    assert len(source_times) == 5
    assert len({row["device_config_id"] for row in wire_records}) == 1
    for wire in wire_records:
        _value, quality = _wire_point(wire)
        assert quality == 1

    truth_deadline = time.monotonic() + 30
    while not all(
        _source_truth_matches(harness, row["sample_time"], _wire_point(row)[0])
        for row in wire_records
    ):
        if time.monotonic() >= truth_deadline:
            raise AssertionError(
                "Kafka data not time/value-aligned with independent Generator truth: "
                f"{[(row['sample_time'], row['points'][POINT]) for row in wire_records]}"
            )
        time.sleep(0.5)

    # Exactly the five observed timestamps must exist with matching values and
    # quality. Each timestamp addresses one complete device row.
    expected = {_time_us(wire["sample_time"]): _wire_point(wire) for wire in wire_records}
    deadline = time.monotonic() + 120
    last_groups: list[dict[str, Any]] = []
    persisted: dict[int, tuple[float, int]] = {}
    while time.monotonic() < deadline:
        try:
            last_groups = _query(harness, start, end)
            stored = _stored_points(last_groups)
            persisted = {timestamp: stored[timestamp] for timestamp in source_times & stored.keys()}
            if len(persisted) == len(source_times):
                break
        except (AssertionError, KeyError, ValueError):
            persisted = {}
        time.sleep(1)
    assert set(persisted) == source_times, {
        "missing_count": len(source_times - set(persisted)),
        "missing_times": sorted(source_times - set(persisted)),
        "last_groups": last_groups,
    }
    for timestamp, (wire_value, wire_quality) in expected.items():
        stored_value, stored_quality = persisted[timestamp]
        assert stored_value == pytest.approx(wire_value, rel=1e-6, abs=1e-5)
        assert stored_quality == wire_quality

    # Restart Storage using the official CLI, then reconcile every persisted
    # value and quality bit at the same five complete-row identities.
    restarted = harness.cli("service", "restart", "storage", "--yes", timeout=120)
    assert restarted["status"] == "SUCCESS", restarted
    harness.wait_targets("storage")
    replay_deadline = time.monotonic() + 90
    replayed: dict[int, tuple[float, int]] = {}
    replay_error: Exception | None = None
    while time.monotonic() < replay_deadline:
        try:
            recovered = _stored_points(_query(harness, start, end))
            replayed = {timestamp: recovered[timestamp] for timestamp in source_times}
            break
        except (AssertionError, KeyError, ValueError) as error:
            replay_error = error
        time.sleep(1)
    assert replayed == persisted, f"Storage restart replay did not converge: {replay_error}"


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
    generation = harness.cli("data-generator", "start", "e2e-plant-physical", "--yes", timeout=90)
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
            reply = harness.cli("commander", "result", command_id, check=False, timeout=30)
            settled = reply.get("data") or {}
            if settled.get("state") in ("SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"):
                break
            time.sleep(0.5)
        if settled.get("state") != "SUCCEEDED":
            token = harness.env["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
            with grpc.insecure_channel("127.0.0.1:52061") as channel:
                execution = generator_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
                    generator_pb.GetInputExecutionRequest(
                        generation_id=generation_id, request_id=command_id
                    ),
                    timeout=10,
                    metadata=(("authorization", f"Bearer {token}"),),
                )
            diag = harness.cli("commander", "events", check=False, timeout=30)
            raise AssertionError(
                f"command {command_id} settled={settled}; "
                f"generator_execution={execution}; commander_events={diag}"
            )
        assert settled.get("state") == "SUCCEEDED", settled
        assert settled.get("outcome") == "CONFIRMED_SUCCESS", settled
        assert settled.get("io_settled") is True, settled

        # Independent authenticated caller directly queries the Generator's
        # durable input-execution evidence. This is not Commander self-report.
        token = harness.env["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
        with grpc.insecure_channel("127.0.0.1:52061") as channel:
            execution = generator_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
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


def test_eight_service_four_sink_outputs_are_independently_readable(
    started: EightServiceHarness,
) -> None:
    """No Sink internal metrics are used as output-delivery proof."""
    harness = started
    listed = harness.cli("sink", "deliveries")
    assert listed["status"] == "SUCCESS", listed
    ids = {
        item.get("delivery_id") or item.get("id")
        for item in (listed.get("data") or {}).get("deliveries", [])
    }
    assert set(DELIVERIES) <= ids
    for delivery_id in DELIVERIES:
        result = harness.cli("sink", "start", delivery_id, "--yes", timeout=90)
        assert result["status"] == "SUCCESS", (delivery_id, result)
    run = harness.cli("data-generator", "start", "e2e-plant-physical", "--yes")
    assert run["status"] == "SUCCESS", run
    generation_id = str(run["data"]["generation_id"])

    async def _check() -> tuple[dict[str, Any], float, int, dict[str, Any]]:
        from pymodbus.client import AsyncModbusTcpClient

        host, _, port = harness.stack.redis.rpartition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        modbus = AsyncModbusTcpClient("127.0.0.1", port=25112)
        broker = AIOKafkaConsumer(
            TOPIC,
            bootstrap_servers=harness.stack.kafka,
            group_id=None,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        await broker.start()
        try:
            deadline = asyncio.get_running_loop().time() + 90
            kafka: dict[str, Any] = {}
            cache_value: dict[str, Any] = {}
            modbus_value: float | None = None
            quality_code: int | None = None
            key = "windhub.e2e.final:e2e-final:collector-e2e-realtime:" f"{DEVICE}:{POINT}"
            while asyncio.get_running_loop().time() < deadline:
                if not kafka:
                    messages = await broker.getmany(timeout_ms=100, max_records=500)
                    for records in messages.values():
                        for record in records:
                            row = json.loads(record.value)
                            points = row.get("points")
                            if (
                                row.get("schema_version") == 3
                                and row.get("data_level") == "protocol"
                                and row.get("device_id") == DEVICE
                                and isinstance(points, dict)
                                and POINT in points
                            ):
                                kafka = row
                                break
                        if kafka:
                            break
                writer.write(f"GET {key}\r\n".encode())
                await writer.drain()
                line = await reader.readline()
                if line.startswith(b"$"):
                    size = int(line[1:-2])
                    raw = await reader.readexactly(size + 2) if size >= 0 else b""
                    if raw:
                        cache_value = json.loads(raw[:-2].decode())
                if not modbus.connected:
                    await modbus.connect()
                if modbus.connected:
                    data = await modbus.read_holding_registers(1000, count=2, device_id=1)
                    quality = await modbus.read_holding_registers(2000, count=1, device_id=1)
                    if not data.isError() and not quality.isError():
                        modbus_value = struct.unpack(">f", struct.pack(">HH", *data.registers))[0]
                        quality_code = int(quality.registers[0])
                if (
                    kafka
                    and cache_value.get("quality") == "good"
                    and modbus_value is not None
                    and quality_code == 0
                ):
                    return kafka, modbus_value, quality_code, cache_value
                await asyncio.sleep(0.5)
            raise AssertionError(
                "four-output evidence incomplete: "
                f"kafka={bool(kafka)} redis={cache_value} "
                f"modbus={modbus_value}/{quality_code}"
            )
        finally:
            await broker.stop()
            modbus.close()
            writer.close()
            await writer.wait_closed()

    try:
        kafka, modbus_value, quality, cache_value = asyncio.run(_check())
        kafka_value, kafka_quality = _wire_point(kafka)
        assert math.isfinite(kafka_value) and kafka_quality == 1
        assert quality == 0
        assert math.isfinite(modbus_value)
        assert math.isfinite(float(cache_value["value"]))

        # The target Gateway is a separate service instance, with a separate
        # operator credential. Query its own exposed variable space.
        token = harness.env["WIND_HUB_E2E_GATEWAY_TARGET_OPERATOR_TOKEN"]
        with grpc.insecure_channel("127.0.0.1:52074") as channel:
            stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
            deadline = time.monotonic() + 30
            while True:
                result = stub.ReadVariables(
                    gateway_pb.ReadVariablesRequest(
                        gateway_id="gw-e2e-sink-target", variable_ids=[POINT]
                    ),
                    timeout=5,
                    metadata=(("authorization", f"Bearer {token}"),),
                )
                if result.values and result.values[0].quality == "GOOD":
                    assert math.isfinite(float(result.values[0].value.double_value))
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("Sink target Gateway has no readable GOOD value")
                time.sleep(0.5)
    finally:
        harness.cli(
            "data-generator",
            "stop",
            generation_id,
            "--yes",
            check=False,
            timeout=60,
        )
