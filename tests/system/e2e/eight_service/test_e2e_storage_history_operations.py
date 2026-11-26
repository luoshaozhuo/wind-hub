"""Real Kafka v3 -> Storage -> TDengine history, aggregate and export acceptance."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest
from aiokafka import AIOKafkaProducer
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as storage_pb
from contracts import storage_service_pb2_grpc as storage_rpc
from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.wide_schema import WideSchema
from tests.system.e2e.eight_service.conftest import EightServiceHarness
from tests.system.e2e.eight_service.test_e2e_eight_service_smoke import _storage_port
from tests.system.e2e.eight_service.test_e2e_storage_capacity import (
    DEVICE_CONFIG_ID,
    _point_value,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.end_to_end]

TOPIC = "windhub.e2e.history"
WTG = "WTG_SIM01"
BESS = "BESS_SIM01"
_POINT_TABLES = {
    WTG: "point_table_modbus_wtg_sim_v1.2.0.yaml",
    BESS: "point_table_modbus_bess_sim_v1.2.0.yaml",
}
_OPERATIONS = {
    "count": storage_pb.STORAGE_AGGREGATION_COUNT,
    "min": storage_pb.STORAGE_AGGREGATION_MIN,
    "max": storage_pb.STORAGE_AGGREGATION_MAX,
    "avg": storage_pb.STORAGE_AGGREGATION_AVG,
}


def _point_types(
    harness: EightServiceHarness, device: str, level: str = "protocol"
) -> dict[str, str]:
    path = harness.root / "configs" / "templates" / "point_tables" / _POINT_TABLES[device]
    document = yaml_safe_load(path)
    business: dict[str, Any] = {}
    if level == "business":
        reference = next(
            item for item in document["config_sources"] if item["config_type"] == "business_points"
        )
        catalog_path = (
            harness.root
            / "configs"
            / "templates"
            / f"business_points_{reference['config_id']}_v{reference['file_version']}.yaml"
        )
        business = yaml_safe_load(catalog_path)["business_points"]
    result: dict[str, str] = {}
    for source_id, definition in document["points"].items():
        point_id = definition["business_point_id"] if level == "business" else source_id
        data_type = (
            business[point_id]["data_type"] if level == "business" else definition["data_type"]
        )
        result[point_id] = str(data_type)
    return result


def yaml_safe_load(path: Path) -> dict[str, Any]:
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _timestamp(moment: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(moment)
    return result


def _filter(
    *,
    level: int,
    start: datetime,
    end: datetime,
    devices: tuple[str, ...] = (),
    points: tuple[str, ...] = (),
    qualities: tuple[int, ...] = (),
) -> storage_pb.StorageFilter:
    return storage_pb.StorageFilter(
        wind_farm_id="e2e-final",
        device_ids=devices,
        data_level=level,
        point_ids=points,
        start_time=_timestamp(start),
        end_time=_timestamp(end),
        qualities=qualities,
    )


def _row(
    harness: EightServiceHarness,
    *,
    device: str,
    level: str,
    sample_time: datetime,
) -> dict[str, Any]:
    points: dict[str, list[Any]] = {}
    for sequence, (point_id, data_type) in enumerate(_point_types(harness, device, level).items()):
        if level == "raw":
            value: Any = "00"
        else:
            value = _point_value(data_type, sequence)
        points[point_id] = [value, 1]
    if device == WTG and level == "protocol":
        points["active_power"] = [123.25, 1]
        points["wind_speed"] = [None, 1]
    elif device == WTG and level == "business":
        points["sim_active_power"] = [456.5, 1]
    return {
        "schema_version": 3,
        "wind_farm_id": "e2e-final",
        "device_config_id": DEVICE_CONFIG_ID,
        "device_id": device,
        "data_level": level,
        "sample_time": sample_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "points": points,
    }


async def _publish(rows: list[dict[str, Any]], bootstrap: str) -> None:
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        acks="all",
        enable_idempotence=True,
        linger_ms=0,
    )
    await producer.start()
    try:
        for row in rows:
            await producer.send_and_wait(
                TOPIC,
                key=f"e2e-final:{row['device_id']}".encode(),
                value=json.dumps(row, separators=(",", ":")).encode(),
            )
    finally:
        await producer.stop()


def _query_all(
    stub: storage_rpc.StorageServiceStub,
    request: storage_pb.QuerySamplesRequest,
    metadata: tuple[tuple[str, str], ...],
) -> tuple[list[storage_pb.StorageGroup], int]:
    groups: list[storage_pb.StorageGroup] = []
    token = ""
    pages = 0
    while True:
        pages += 1
        assert pages <= 100, "QuerySamples pagination did not converge"
        reply = stub.QuerySamples(
            storage_pb.QuerySamplesRequest(
                filter=request.filter,
                page_size=request.page_size,
                page_token=token,
            ),
            metadata=metadata,
            timeout=20,
        )
        groups.extend(reply.groups)
        if reply.complete:
            assert not reply.next_page_token
            return groups, pages
        assert reply.next_page_token and reply.next_page_token != token
        token = reply.next_page_token


def _wait_protocol_groups(
    stub: storage_rpc.StorageServiceStub,
    request: storage_pb.QuerySamplesRequest,
    metadata: tuple[tuple[str, str], ...],
) -> tuple[list[storage_pb.StorageGroup], int]:
    deadline = time.monotonic() + 120
    latest: Any = None
    while time.monotonic() < deadline:
        try:
            groups, pages = _query_all(stub, request, metadata)
            devices = {group.device_id for group in groups}
            schemas = {group.schema_fingerprint for group in groups}
            if {WTG, BESS} <= devices and len(schemas) >= 2:
                return groups, pages
            latest = (devices, schemas)
        except grpc.RpcError as exc:
            latest = exc
        time.sleep(1)
    raise AssertionError(f"timed out waiting for two real schema groups: {latest}")


def _aggregate_one(
    stub: storage_rpc.StorageServiceStub,
    *,
    operation: int,
    filter_: storage_pb.StorageFilter,
    metadata: tuple[tuple[str, str], ...],
) -> storage_pb.StorageAggregate:
    token = ""
    items: list[storage_pb.StorageAggregate] = []
    for _ in range(100):
        reply = stub.QueryAggregates(
            storage_pb.QueryAggregatesRequest(
                filter=filter_,
                aggregation=operation,
                window_ms=1,
                page_size=1,
                page_token=token,
            ),
            metadata=metadata,
            timeout=20,
        )
        items.extend(reply.items)
        if reply.complete:
            assert not reply.next_page_token
            break
        assert reply.next_page_token and reply.next_page_token != token
        token = reply.next_page_token
    assert len(items) == 1, items
    return items[0]


def _wait_export(
    stub: storage_rpc.StorageServiceStub,
    *,
    request_id: str,
    filter_: storage_pb.StorageFilter,
    export_format: str,
    metadata: tuple[tuple[str, str], ...],
) -> storage_pb.StorageTask:
    task = stub.CreateExport(
        storage_pb.CreateExportRequest(
            request_id=request_id,
            filter=filter_,
            export_format=export_format,
        ),
        metadata=metadata,
        timeout=20,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if task.state == storage_pb.STORAGE_TASK_STATE_SUCCEEDED:
            assert task.integrity_verified
            assert task.artifact_path
            artifact = Path(task.artifact_path)
            assert artifact.stat().st_size == task.size_bytes
            assert hashlib.sha256(artifact.read_bytes()).hexdigest() == task.sha256
            return task
        assert task.state not in {
            storage_pb.STORAGE_TASK_STATE_FAILED,
            storage_pb.STORAGE_TASK_STATE_INTERRUPTED,
        }, task
        time.sleep(0.2)
        task = stub.GetExportTask(
            storage_pb.GetStorageTaskRequest(task_id=task.task_id),
            metadata=metadata,
            timeout=20,
        )
    raise AssertionError(f"export did not finish: {task}")


def _assert_tdengine_rows(
    harness: EightServiceHarness, *, start_us: int, end_us: int, expected: int
) -> None:
    database = WideSchema.database_name("e2e-final")
    count = 0
    for stable, *_ in harness.stack.tdengine_sql(f"SHOW `{database}`.STABLES", database=database)[
        "data"
    ]:
        rows = harness.stack.tdengine_sql(
            f"SELECT COUNT(*) FROM `{database}`.`{stable}` "
            f"WHERE ts >= {start_us} AND ts < {end_us}",
            database=database,
        )["data"]
        count += int(rows[0][0])
    assert count == expected


def test_real_history_aggregate_and_export_operations(started: EightServiceHarness) -> None:
    harness = started
    base = datetime.now(UTC)
    rows = [
        _row(harness, device=WTG, level="protocol", sample_time=base),
        _row(harness, device=BESS, level="protocol", sample_time=base + timedelta(microseconds=1)),
        _row(harness, device=WTG, level="raw", sample_time=base + timedelta(microseconds=2)),
        _row(harness, device=WTG, level="business", sample_time=base + timedelta(microseconds=3)),
    ]
    asyncio.run(_publish(rows, harness.stack.kafka))
    start = base - timedelta(seconds=1)
    end = base + timedelta(seconds=2)
    metadata = (("authorization", f"Bearer {harness.env['WIND_HUB_STORAGE_OPERATOR_TOKEN']}"),)

    with grpc.insecure_channel(f"127.0.0.1:{_storage_port(harness)}") as channel:
        stub = storage_rpc.StorageServiceStub(channel)
        protocol_request = storage_pb.QuerySamplesRequest(
            filter=_filter(
                level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
                start=start,
                end=end,
                devices=(WTG, BESS),
            ),
            page_size=1,
        )
        blocked_deadline = time.monotonic() + 10
        while time.monotonic() < blocked_deadline:
            blocked = stub.ListBlockedPartitions(
                storage_pb.ListBlockedPartitionsRequest(page_size=100),
                metadata=metadata,
                timeout=10,
            )
            if blocked.items:
                raise AssertionError(f"Storage rejected injected rows: {blocked.items}")
            time.sleep(0.2)
        groups, pages = _wait_protocol_groups(stub, protocol_request, metadata)
        assert pages >= 2
        assert {group.device_id for group in groups} == {WTG, BESS}
        assert len({group.schema_fingerprint for group in groups}) == 2
        for group in groups:
            assert len(group.rows) == 1
            row = group.rows[0]
            assert len(row.values) == len(group.columns)
            assert len(unpack_quality_bitmap(row.quality, len(group.columns))) == len(group.columns)
        wtg = next(group for group in groups if group.device_id == WTG)
        wtg_columns = [column.point_id for column in wtg.columns]
        wtg_row = wtg.rows[0]
        power = wtg_row.values[wtg_columns.index("active_power")]
        wind = wtg_row.values[wtg_columns.index("wind_speed")]
        assert power.WhichOneof("value") == "value_f64" and power.value_f64 == 123.25
        assert wind.WhichOneof("value") is None
        assert (
            unpack_quality_bitmap(wtg_row.quality, len(wtg_columns))[
                wtg_columns.index("wind_speed")
            ]
            == 1
        )

        for level, field, expected in (
            (storage_pb.STORAGE_DATA_LEVEL_RAW, "payload_bytes", b"\x00"),
            (storage_pb.STORAGE_DATA_LEVEL_BUSINESS, "value_f64", 456.5),
        ):
            level_groups, _ = _query_all(
                stub,
                storage_pb.QuerySamplesRequest(
                    filter=_filter(level=level, start=start, end=end, devices=(WTG,)),
                    page_size=10,
                ),
                metadata,
            )
            assert len(level_groups) == 1 and len(level_groups[0].rows) == 1
            group = level_groups[0]
            columns = [column.point_id for column in group.columns]
            value = group.rows[0].values[columns.index("active_power")]
            assert value.WhichOneof("value") == field
            assert getattr(value, field) == expected

        for name, operation in _OPERATIONS.items():
            item = _aggregate_one(
                stub,
                operation=operation,
                filter_=_filter(
                    level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
                    start=start,
                    end=end,
                    devices=(WTG,),
                    points=("active_power",),
                ),
                metadata=metadata,
            )
            assert item.good_count == 1
            assert item.missing_count == 0
            assert item.valid_count == 1
            if name == "count":
                assert item.result.WhichOneof("value") == "value_u64"
                assert item.result.value_u64 == 1
            else:
                assert item.result.WhichOneof("value") == "value_f64"
                assert item.result.value_f64 == pytest.approx(123.25)

            null_item = _aggregate_one(
                stub,
                operation=operation,
                filter_=_filter(
                    level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
                    start=start,
                    end=end,
                    devices=(WTG,),
                    points=("wind_speed",),
                ),
                metadata=metadata,
            )
            assert null_item.good_count == 0
            assert null_item.missing_count == 1
            assert null_item.valid_count == 0
            if name == "count":
                assert null_item.result.value_u64 == 0
            else:
                assert null_item.result.WhichOneof("value") is None

            bad_item = _aggregate_one(
                stub,
                operation=operation,
                filter_=_filter(
                    level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
                    start=start,
                    end=end,
                    devices=(WTG,),
                    points=("active_power",),
                    qualities=(storage_pb.STORAGE_QUALITY_BAD,),
                ),
                metadata=metadata,
            )
            assert bad_item.good_count == 1
            assert bad_item.valid_count == 0
            if name == "count":
                assert bad_item.result.value_u64 == 0
            else:
                assert bad_item.result.WhichOneof("value") is None

        export_filter = _filter(
            level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
            start=start,
            end=end,
            devices=(WTG, BESS),
        )
        json_task = _wait_export(
            stub,
            request_id="d5-r2-json-export",
            filter_=export_filter,
            export_format="json",
            metadata=metadata,
        )
        document = json.loads(Path(json_task.artifact_path).read_text(encoding="utf-8"))
        assert {group["device_id"] for group in document["groups"]} == {WTG, BESS}
        assert sum(len(group["rows"]) for group in document["groups"]) == 2

        csv_task = _wait_export(
            stub,
            request_id="d5-r2-csv-export",
            filter_=export_filter,
            export_format="csv",
            metadata=metadata,
        )
        with zipfile.ZipFile(csv_task.artifact_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert len(manifest) == 2
            csv_rows = [
                row
                for entry in manifest
                for row in csv.reader(io.StringIO(archive.read(entry["file"]).decode()))
            ]
            assert sum(1 for row in csv_rows if row and row[0] == "e2e-final") == 2

        with pytest.raises(grpc.RpcError) as backup:
            stub.CreateBackup(
                storage_pb.CreateBackupRequest(
                    request_id="d5-r2-backup-disabled",
                    wind_farm_id="e2e-final",
                ),
                metadata=metadata,
                timeout=20,
            )
        assert backup.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    start_us = int(start.timestamp() * 1_000_000)
    end_us = int(end.timestamp() * 1_000_000)
    _assert_tdengine_rows(harness, start_us=start_us, end_us=end_us, expected=4)
