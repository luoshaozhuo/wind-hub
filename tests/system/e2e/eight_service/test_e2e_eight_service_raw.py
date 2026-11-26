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
from storage_service.domain.wide_schema import WideSchema
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


def _query_groups(
    stub: Any,
    request: storage_pb.QuerySamplesRequest,
    metadata: tuple[tuple[str, str], ...],
) -> list[storage_pb.StorageGroup]:
    groups: list[storage_pb.StorageGroup] = []
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(1000):
        reply = stub.QuerySamples(
            storage_pb.QuerySamplesRequest(
                filter=request.filter,
                page_size=request.page_size,
                page_token=page_token,
            ),
            timeout=15,
            metadata=metadata,
        )
        groups.extend(reply.groups)
        if not reply.next_page_token:
            assert reply.complete
            return groups
        assert not reply.complete and reply.next_page_token not in seen_tokens
        seen_tokens.add(reply.next_page_token)
        page_token = reply.next_page_token
    raise AssertionError("Storage grouped RAW query exceeded 1000 pages")


def _physical_table(
    harness: EightServiceHarness,
    database: str,
    group: storage_pb.StorageGroup,
) -> str:
    """按服务端完整点表与 Schema 标签定位子表，不重建废弃版本字段。"""
    stables = harness.stack.tdengine_sql(f"SHOW `{database}`.STABLES", database=database)["data"]
    matches: list[str] = []
    for stable, *_ in stables:
        rows = harness.stack.tdengine_sql(
            f"SELECT TBNAME FROM `{database}`.`{stable}` "
            f"WHERE device_id = '{DEVICE}' "
            f"AND point_table_id = '{group.point_table_id}' "
            f"AND schema_fingerprint = '{group.schema_fingerprint}' LIMIT 1",
            database=database,
        )["data"]
        matches.extend(str(row[0]) for row in rows)
    assert len(matches) == 1, (group.point_table_id, group.schema_fingerprint, matches)
    return matches[0]


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
                f"only {len(selected)} FC04 RAW rows independently matched; " f"observed={observed}"
            )
        finally:
            await consumer.stop()

    wires, generation_id = asyncio.run(collect())
    assert len(wires) == 3
    config_ids = {row["device_config_id"] for row in wires}
    assert config_ids == {"device_e2e-final_v1.0.1"}
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
    last_groups: list[storage_pb.StorageGroup] = []
    request = storage_pb.QuerySamplesRequest(
        filter=storage_pb.StorageFilter(
            wind_farm_id="e2e-final",
            device_ids=(DEVICE,),
            data_level=storage_pb.STORAGE_DATA_LEVEL_RAW,
            point_ids=(POINT,),
            start_time=start_ts,
            end_time=end_ts,
        ),
        page_size=1000,
    )
    with grpc.insecure_channel(f"127.0.0.1:{_storage_port(harness)}") as channel:
        stub = storage_rpc.StorageServiceStub(channel)
        while time.monotonic() < deadline:
            last_groups = _query_groups(stub, request, metadata)
            for group in last_groups:
                columns = tuple(column.point_id for column in group.columns)
                if POINT not in columns:
                    continue
                point_index = columns.index(POINT)
                for row in group.rows:
                    assert len(row.values) == len(columns)
                    value = row.values[point_index]
                    if value.WhichOneof("value") == "payload_bytes":
                        timestamp = _unix_us(row.sample_time.ToDatetime(tzinfo=UTC))
                        observed.add((timestamp, bytes(value.payload_bytes)))
            if expected <= observed:
                break
            time.sleep(1)
    assert (
        expected <= observed
    ), f"Storage grouped RPC did not return RAW points: {expected - observed}"
    selected_group = next(
        group
        for group in last_groups
        if group.schema_fingerprint and any(column.point_id == POINT for column in group.columns)
    )
    assert selected_group.point_table_id == "point_table_modbus_wtg_sim_v1.2.0"
    point_column = next(
        column.physical_name for column in selected_group.columns if column.point_id == POINT
    )
    assert point_column
    database = WideSchema.database_name("e2e-final")
    table = _physical_table(harness, database, selected_group)
    for timestamp, raw in expected:
        rows = harness.stack.tdengine_sql(
            f"SELECT `{point_column}` FROM `{database}`.`{table}` WHERE ts = {timestamp}",
            database=database,
        )["data"]
        assert len(rows) == 1, (timestamp, rows)
        assert _physical_bytes(rows[0][0]) == raw

    harness.cli("data-generator", "stop", generation_id, "--yes", check=False)
