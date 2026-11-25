"""FC04 RAW through the real eight-service Storage and TDengine boundary."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from aiokafka import AIOKafkaConsumer
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as storage_pb
from contracts import storage_service_pb2_grpc as storage_rpc
from tests.system.e2e.eight_service.conftest import EightServiceHarness
from tests.system.e2e.eight_service.test_e2e_eight_service_smoke import (
    _storage_port,
    _tdengine_row_count,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.end_to_end]

DEVICE = "WTG_SIM01"
POINT = "active_power"
TOPIC = "windhub.e2e.history"


@pytest.fixture
def raw_started(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[Any]:
    monkeypatch.setenv("EIGHT_SERVICE_SINK_DATA_LEVEL", "raw")
    yield request.getfixturevalue("started")


def _truth(root: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in (root / "artifacts" / "e2e-final" / "data-generator").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for point in json.loads(line)["points"]:
                if point["device_id"] == DEVICE and point["point_id"] == POINT:
                    result.append(point)
    return result


def _expected_fc04_bytes(value: float) -> bytes:
    # The active PointTable uses little word order. RAW is the actual FC04
    # register data field in received word order, not a complete Modbus ADU.
    encoded = struct.pack(">f", value)
    words = [encoded[index : index + 2] for index in range(0, 4, 2)]
    return b"".join(reversed(words))


def _truth_matches(harness: EightServiceHarness, sample_time: str, raw: bytes) -> bool:
    sampled = datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
    for point in _truth(harness.root):
        if not isinstance(point["value"], int | float):
            continue
        lag = (sampled - datetime.fromisoformat(point["source_time"])).total_seconds()
        if 0 <= lag <= 2 and _expected_fc04_bytes(float(point["value"])) == raw:
            return True
    return False


def test_fc04_raw_reaches_storage_and_tdengine(raw_started: EightServiceHarness) -> None:
    harness = raw_started
    result = harness.cli("sink", "start", "e2e_kafka_protocol", "--yes", timeout=90)
    assert result["status"] == "SUCCESS", result
    started_at = datetime.now(UTC)

    async def collect() -> tuple[list[dict[str, Any]], str]:
        consumer = AIOKafkaConsumer(
            TOPIC,
            bootstrap_servers=harness.stack.kafka,
            group_id=None,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            deadline = asyncio.get_running_loop().time() + 15
            while not consumer.assignment():
                await consumer.getmany(timeout_ms=250)
                assert asyncio.get_running_loop().time() < deadline
            drain_deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < drain_deadline:
                if not await consumer.getmany(timeout_ms=250, max_records=1000):
                    break
            created = harness.cli(
                "data-generator", "start", "e2e-plant-physical", "--yes", timeout=90
            )
            assert created["status"] == "SUCCESS", created
            selected: dict[str, dict[str, Any]] = {}
            observed: list[dict[str, Any]] = []
            deadline = asyncio.get_running_loop().time() + 90
            while len(selected) < 3 and asyncio.get_running_loop().time() < deadline:
                messages = await consumer.getmany(timeout_ms=1000, max_records=500)
                for records in messages.values():
                    for record in records:
                        row = json.loads(record.value)
                        if (
                            row.get("data_level") == "raw"
                            and row.get("collector_id") == "collector-e2e-reliable"
                            and row.get("device_id") == DEVICE
                            and row.get("point_id") == POINT
                        ):
                            if len(observed) < 3:
                                observed.append(row)
                            if _truth_matches(
                                harness, row["sample_time"], bytes.fromhex(row["value"])
                            ):
                                selected[row["sample_id"]] = row
                                if len(selected) == 3:
                                    return list(selected.values()), str(
                                        created["data"]["generation_id"]
                                    )
            expected = [
                {
                    "source_time": point["source_time"],
                    "raw": _expected_fc04_bytes(point["value"]).hex(),
                }
                for point in _truth(harness.root)[:3]
            ]
            raise AssertionError(
                f"only {len(selected)} independently matched FC04 RAW samples; "
                f"observed={observed}; expected={expected}"
            )
        finally:
            await consumer.stop()

    wires, generation_id = asyncio.run(collect())
    assert len(wires) == 3
    for wire in wires:
        raw = bytes.fromhex(wire["value"])
        assert len(raw) == 4
        assert wire["task_id"] == "e2e_source_all"
        assert wire["sample_id"] and wire["batch_id"] and wire["point_table_id"]

    token = harness.env["WIND_HUB_STORAGE_OPERATOR_TOKEN"]
    start_ts, end_ts = Timestamp(), Timestamp()
    start_ts.FromDatetime(started_at - timedelta(minutes=1))
    end_ts.FromDatetime(started_at + timedelta(minutes=4))
    metadata = (("authorization", f"Bearer {token}"),)
    deadline = time.monotonic() + 90
    stored: dict[str, Any] = {}
    with grpc.insecure_channel(f"127.0.0.1:{_storage_port(harness)}") as channel:
        stub = storage_rpc.StorageServiceStub(channel)
        while time.monotonic() < deadline:
            reply = stub.QuerySamples(
                storage_pb.QuerySamplesRequest(
                    filter=storage_pb.StorageFilter(
                        wind_farm_id="e2e-final",
                        device_id=DEVICE,
                        point_id=POINT,
                        data_level=storage_pb.STORAGE_DATA_LEVEL_RAW,
                        start_time=start_ts,
                        end_time=end_ts,
                    ),
                    page_size=1000,
                ),
                timeout=15,
                metadata=metadata,
            )
            stored = {
                item.sample_id: item
                for item in reply.items
                if item.sample_id in {w["sample_id"] for w in wires}
            }
            if len(stored) == 3:
                break
            time.sleep(1)
    assert set(stored) == {wire["sample_id"] for wire in wires}
    for wire in wires:
        item = stored[wire["sample_id"]]
        assert item.payload_bytes == bytes.fromhex(wire["value"])
        assert item.data_level == storage_pb.STORAGE_DATA_LEVEL_RAW

    # Independently read the physical TDengine payload, not only Storage RPC.
    physical = _tdengine_row_count(harness)
    database = str(physical["database"])
    stable = str(physical["stable"])
    for wire in wires:
        rows = harness.stack.tdengine_sql(
            f"SELECT sample_id, payload_bytes FROM `{database}`.`{stable}` "
            f"WHERE sample_id='{wire['sample_id']}'",
            database=database,
        )["data"]
        assert len(rows) == 1, (wire["sample_id"], rows)
        returned = rows[0][1]
        if isinstance(returned, str):
            returned_bytes = bytes.fromhex(returned.removeprefix("\\x"))
        else:
            returned_bytes = bytes(returned)
        assert returned_bytes == bytes.fromhex(wire["value"])

    harness.cli("data-generator", "stop", generation_id, "--yes", check=False)
