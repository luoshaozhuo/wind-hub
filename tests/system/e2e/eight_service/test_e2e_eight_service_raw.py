"""FC04 RAW bytes through real Kafka v3, Storage wide RPC and TDengine."""

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
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from tests.system.e2e.eight_service.conftest import EightServiceHarness
from tests.system.e2e.eight_service.test_e2e_eight_service_smoke import _storage_port

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
    encoded = struct.pack(">f", value)
    return encoded[2:4] + encoded[0:2]


def _unix_us(moment: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = moment.astimezone(UTC) - epoch
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _sample_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _truth_matches(harness: EightServiceHarness, sample_time: str, raw: bytes) -> bool:
    sampled = _sample_time(sample_time)
    for point in _truth(harness.root):
        if not isinstance(point["value"], int | float):
            continue
        lag = (sampled - _sample_time(point["source_time"])).total_seconds()
        if 0 <= lag <= 2 and _expected_fc04_bytes(float(point["value"])) == raw:
            return True
    return False


def _physical_bytes(value: object) -> bytes:
    if isinstance(value, str):
        normalized = value.removeprefix("0x").removeprefix("\\x")
        return bytes.fromhex(normalized)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    raise AssertionError(f"TDengine returned unsupported RAW representation: {type(value)}")


def test_fc04_raw_reaches_storage_and_tdengine(raw_started: EightServiceHarness) -> None:
    harness = raw_started
    delivery = harness.cli("sink", "start", "e2e_kafka_protocol", "--yes", timeout=90)
    assert delivery["status"] == "SUCCESS", delivery
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
            drain_until = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < drain_until:
                if not await consumer.getmany(timeout_ms=250, max_records=1000):
                    break
            created = harness.cli(
                "data-generator", "start", "e2e-plant-physical", "--yes", timeout=90
            )
            assert created["status"] == "SUCCESS", created
            selected: dict[tuple[str, str], dict[str, Any]] = {}
            observed: list[dict[str, Any]] = []
            deadline = asyncio.get_running_loop().time() + 90
            while len(selected) < 3 and asyncio.get_running_loop().time() < deadline:
                messages = await consumer.getmany(timeout_ms=1000, max_records=500)
                for records in messages.values():
                    for record in records:
                        row = json.loads(record.value)
                        if (
                            row.get("schema_version") != 3
                            or row.get("data_level") != "raw"
                            or row.get("wind_farm_id") != "e2e-final"
                            or row.get("device_id") != DEVICE
                        ):
                            continue
                        points = row.get("points")
                        if not isinstance(points, dict) or POINT not in points:
                            continue
                        entry = points[POINT]
                        if not isinstance(entry, list) or len(entry) != 2:
                            continue
                        raw_hex, quality = entry
                        if not isinstance(raw_hex, str) or quality != 1:
                            continue
                        raw = bytes.fromhex(raw_hex)
                        if len(raw) != 4:
                            continue
                        if len(observed) < 3:
                            observed.append(row)
                        if _truth_matches(harness, row["sample_time"], raw):
                            selected[(row["sample_time"], raw_hex)] = row
                            if len(selected) == 3:
                                return list(selected.values()), str(
                                    created["data"]["generation_id"]
                                )
            raise AssertionError(
                f"only {len(selected)} FC04 RAW rows independently matched; "
                f"observed={observed}"
            )
        finally:
            await consumer.stop()

    wires, generation_id = asyncio.run(collect())
    assert len(wires) == 3
    config_ids = {row["device_config_id"] for row in wires}
    assert len(config_ids) == 1
    config_id = next(iter(config_ids))
    expected = {
        (_unix_us(_sample_time(row["sample_time"])), bytes.fromhex(row["points"][POINT][0]))
        for row in wires
    }

    token = harness.env["WIND_HUB_STORAGE_OPERATOR_TOKEN"]
    start_ts, end_ts = Timestamp(), Timestamp()
    start_ts.FromDatetime(started_at - timedelta(minutes=1))
    end_ts.FromDatetime(started_at + timedelta(minutes=4))
    metadata = (("authorization", f"Bearer {token}"),)
    deadline = time.monotonic() + 90
    observed: set[tuple[int, bytes]] = set()
    last_reply: storage_pb.QueryWideRowsResponse | None = None
    with grpc.insecure_channel(f"127.0.0.1:{_storage_port(harness)}") as channel:
        stub = storage_rpc.StorageServiceStub(channel)
        while time.monotonic() < deadline:
            reply = stub.QueryWideRows(
                storage_pb.QueryWideRowsRequest(
                    wind_farm_id="e2e-final",
                    device_config_id=config_id,
                    device_id=DEVICE,
                    data_level=storage_pb.STORAGE_DATA_LEVEL_RAW,
                    start_time=start_ts,
                    end_time=end_ts,
                    page_size=1000,
                ),
                timeout=15,
                metadata=metadata,
            )
            last_reply = reply
            for row in reply.rows:
                timestamp = _unix_us(row.sample_time.ToDatetime(tzinfo=UTC))
                for point in row.points:
                    if point.point_id == POINT and point.WhichOneof("value") == "bytes_value":
                        observed.add((timestamp, bytes(point.bytes_value)))
            if expected <= observed:
                break
            time.sleep(1)
    assert expected <= observed, f"Storage wide RPC did not return RAW points: {expected - observed}"
    assert last_reply is not None
    assert last_reply.schema_fingerprint
    assert last_reply.point_table_version

    columns = tuple(PhysicalPoint(item.point_id, item.sql_type) for item in last_reply.columns)
    schema = WideSchema.build(
        point_table_id=last_reply.point_table_id,
        point_table_version=last_reply.point_table_version,
        data_level=WideDataLevel.RAW,
        points=columns,
        max_row_bytes=65536,
    )
    assert schema.fingerprint == last_reply.schema_fingerprint
    point_column = next(
        col.column_name for col in schema.columns if col.point_id == POINT
    )
    database = schema.database_name("e2e-final")
    table = schema.subtable_name(DEVICE)
    for timestamp, raw in expected:
        rows = harness.stack.tdengine_sql(
            f"SELECT `{point_column}` FROM `{database}`.`{table}` "
            f"WHERE ts = {timestamp}",
            database=database,
        )["data"]
        assert len(rows) == 1, (timestamp, rows)
        assert _physical_bytes(rows[0][0]) == raw

    harness.cli("data-generator", "stop", generation_id, "--yes", check=False)
