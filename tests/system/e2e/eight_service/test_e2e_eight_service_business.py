"""Real upstream BUSINESS mapping through Kafka v3, Storage RPC and TDengine."""

from __future__ import annotations

import asyncio
import json
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
from tests.system.e2e.eight_service.test_e2e_eight_service_raw import (
    _physical_table,
    _query_groups,
    _sample_time,
    _truth,
    _unix_us,
)
from tests.system.e2e.eight_service.test_e2e_eight_service_smoke import _storage_port

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.end_to_end]

DEVICE = "WTG_SIM01"
SOURCE_POINT = "active_power"
BUSINESS_POINT = "sim_active_power"
TOPIC = "windhub.e2e.history"


@pytest.fixture
def business_started(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Any]:
    monkeypatch.setenv("EIGHT_SERVICE_SINK_DATA_LEVEL", "business")
    yield request.getfixturevalue("started")


def _truth_matches(harness: EightServiceHarness, sample_time: str, value: float) -> bool:
    sampled = _sample_time(sample_time)
    for point in _truth(harness.root):
        if not isinstance(point["value"], int | float):
            continue
        lag = (sampled - _sample_time(point["source_time"])).total_seconds()
        if 0 <= lag <= 2 and abs(float(point["value"]) - value) <= max(1e-3, abs(value) * 2e-3):
            return True
    return False


def test_business_mapping_reaches_storage_and_tdengine(
    business_started: EightServiceHarness,
) -> None:
    """仿真映射证明代码路径，不将配置哈希解释为 OEM 语义批准。"""
    harness = business_started
    delivery = harness.cli("sink", "start", "e2e_kafka_protocol", "--yes", timeout=90)
    assert delivery["status"] == "SUCCESS", delivery
    started_at = datetime.now(UTC)

    async def collect() -> tuple[dict[str, Any], str]:
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
            deadline = asyncio.get_running_loop().time() + 90
            while asyncio.get_running_loop().time() < deadline:
                messages = await consumer.getmany(timeout_ms=1000, max_records=500)
                for records in messages.values():
                    for record in records:
                        row = json.loads(record.value)
                        if (
                            row.get("schema_version") != 3
                            or row.get("data_level") != "business"
                            or row.get("device_config_id") != "device_e2e-final_v1.0.1"
                            or row.get("device_id") != DEVICE
                        ):
                            continue
                        entry = row.get("points", {}).get(BUSINESS_POINT)
                        if (
                            isinstance(entry, list)
                            and len(entry) == 2
                            and type(entry[0]) in (int, float)
                            and entry[1] == 1
                            and _truth_matches(harness, row["sample_time"], float(entry[0]))
                        ):
                            return row, str(created["data"]["generation_id"])
            raise AssertionError("no independently matched live BUSINESS Kafka row")
        finally:
            await consumer.stop()

    wire, generation_id = asyncio.run(collect())
    expected_time = _unix_us(_sample_time(wire["sample_time"]))
    expected_value = float(wire["points"][BUSINESS_POINT][0])
    token = harness.env["WIND_HUB_STORAGE_OPERATOR_TOKEN"]
    start_ts, end_ts = Timestamp(), Timestamp()
    start_ts.FromDatetime(started_at - timedelta(minutes=1))
    end_ts.FromDatetime(started_at + timedelta(minutes=4))
    metadata = (("authorization", f"Bearer {token}"),)
    request = storage_pb.QuerySamplesRequest(
        filter=storage_pb.StorageFilter(
            wind_farm_id="e2e-final",
            device_ids=(DEVICE,),
            data_level=storage_pb.STORAGE_DATA_LEVEL_BUSINESS,
            point_ids=(BUSINESS_POINT,),
            start_time=start_ts,
            end_time=end_ts,
        ),
        page_size=1000,
    )
    with grpc.insecure_channel(f"127.0.0.1:{_storage_port(harness)}") as channel:
        stub = storage_rpc.StorageServiceStub(channel)
        deadline = time.monotonic() + 90
        selected_group: storage_pb.StorageGroup | None = None
        while time.monotonic() < deadline:
            groups = _query_groups(stub, request, metadata)
            for group in groups:
                columns = tuple(column.point_id for column in group.columns)
                if SOURCE_POINT not in columns:
                    continue
                index = columns.index(SOURCE_POINT)
                for row in group.rows:
                    if _unix_us(row.sample_time.ToDatetime(tzinfo=UTC)) != expected_time:
                        continue
                    value = row.values[index]
                    if value.WhichOneof(
                        "value"
                    ) == "value_f64" and value.value_f64 == pytest.approx(
                        expected_value, rel=1e-6, abs=1e-5
                    ):
                        selected_group = group
                        break
                if selected_group is not None:
                    break
            if selected_group is not None:
                break
            time.sleep(1)
    assert selected_group is not None, "Storage RPC did not return the live BUSINESS row"
    assert selected_group.point_table_id == "point_table_modbus_wtg_sim_v1.2.0"
    physical = next(
        column.physical_name for column in selected_group.columns if column.point_id == SOURCE_POINT
    )
    database = WideSchema.database_name("e2e-final")
    table = _physical_table(harness, database, selected_group)
    rows = harness.stack.tdengine_sql(
        f"SELECT `{physical}` FROM `{database}`.`{table}` WHERE ts = {expected_time}",
        database=database,
    )["data"]
    assert len(rows) == 1
    assert float(rows[0][0]) == pytest.approx(expected_value, rel=1e-6, abs=1e-5), rows

    harness.cli("data-generator", "stop", generation_id, "--yes", check=False)
