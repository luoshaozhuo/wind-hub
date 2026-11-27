"""Single Storage instance Kafka v3 complete-row -> TDengine capacity evidence."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml
from aiokafka import AIOKafkaProducer
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as storage_pb
from contracts import storage_service_pb2_grpc as storage_rpc
from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.wide_record import WideRecord
from storage_service.domain.wide_schema import WideSchema
from tests.fixtures.services.r5_stack import R5StackError
from tests.system.e2e.eight_service.conftest import EightServiceHarness, _ensure_topic
from tests.system.e2e.eight_service.test_e2e_eight_service_smoke import _storage_port

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.capacity]

DEVICE = "WTG_SIM01"
DEVICE_CONFIG_ID = "device_e2e-final_v1.0.1"


def _capacity_point_types(
    harness: EightServiceHarness,
) -> tuple[str, dict[str, str]]:
    path = (
        harness.root
        / "configs"
        / "templates"
        / "point_tables"
        / "point_table_modbus_wtg_sim_v1.2.0.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    point_table_id = f"point_table_{document['template_id']}_v{document['file_version']}"
    assert point_table_id == "point_table_modbus_wtg_sim_v1.2.0"
    return point_table_id, {
        point_id: str(definition["data_type"])
        for point_id, definition in document["points"].items()
    }


def _point_value(data_type: str, sequence: int) -> int | float:
    if data_type in {"float32", "float64"}:
        return float(sequence % 1000) + 0.25
    if data_type in {
        "int8",
        "uint8",
        "int16",
        "uint16",
        "int32",
        "uint32",
        "int64",
        "uint64",
    }:
        return sequence % 1000
    raise AssertionError(f"capacity fixture needs native value support for {data_type}")


def _count_capacity_rows(
    harness: EightServiceHarness,
    *,
    database: str,
    point_table_id: str,
    start_us: int,
    end_us: int,
) -> int:
    stables = harness.stack.tdengine_sql(f"SHOW `{database}`.STABLES", database=database)["data"]
    count = 0
    for stable, *_ in stables:
        rows = harness.stack.tdengine_sql(
            f"SELECT COUNT(*) FROM `{database}`.`{stable}` "
            f"WHERE device_id = '{DEVICE}' AND point_table_id = '{point_table_id}' "
            f"AND ts >= {start_us} AND ts < {end_us}",
            database=database,
            timeout=30,
        )["data"]
        count += int(rows[0][0])
    return count


def test_storage_single_instance_capacity_60000_values_per_second(
    started: EightServiceHarness,
) -> None:
    harness = started
    topic = "windhub.e2e.history"
    batches = int(os.environ.get("STORAGE_CAPACITY_BATCHES", "3600"))
    rate = float(os.environ.get("STORAGE_CAPACITY_BATCH_RATE", "120"))
    if batches <= 0 or rate <= 0:
        raise ValueError("positive capacity load required")
    nominal_points_per_batch = 500
    point_table_id, point_types = _capacity_point_types(harness)
    # E2E 的真实 WTG 点表只有 32 点；用多条合法完整行达到原容量批次，
    # 不能重复同一 point_id 或伪造 500 个配置外测点。
    records_per_batch = (nominal_points_per_batch + len(point_types) - 1) // len(point_types)
    expected_rows = batches * records_per_batch

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
        sample_start = datetime.now(UTC)
        sample_start_us = int(sample_start.timestamp()) * 1_000_000 + sample_start.microsecond
        sample_end_us = sample_start_us + expected_rows
        try:
            started_at = time.monotonic()
            next_send = started_at
            pending: set[asyncio.Task[object]] = set()
            for batch_index in range(batches):
                builder = producer.create_batch()
                for sequence in range(records_per_batch):
                    row_sequence = batch_index * records_per_batch + sequence
                    sample_time = (
                        (sample_start + timedelta(microseconds=row_sequence))
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z")
                    )
                    payload = {
                        "schema_version": 3,
                        "wind_farm_id": "e2e-final",
                        "device_config_id": DEVICE_CONFIG_ID,
                        "device_id": DEVICE,
                        "data_level": "protocol",
                        "sample_time": sample_time,
                        "points": {
                            point_id: [_point_value(data_type, row_sequence), 1]
                            for point_id, data_type in point_types.items()
                        },
                    }
                    assert (
                        builder.append(
                            key=f"e2e-final:{DEVICE}".encode(),
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
            database = WideSchema.database_name("e2e-final")
            deadline = time.monotonic() + 180
            last_count = 0
            while time.monotonic() < deadline:
                try:
                    last_count = _count_capacity_rows(
                        harness,
                        database=database,
                        point_table_id=point_table_id,
                        start_us=sample_start_us,
                        end_us=sample_end_us,
                    )
                except R5StackError:
                    await asyncio.sleep(1)
                    continue
                if last_count >= expected_rows:
                    break
                await asyncio.sleep(2)
            drained_at = time.monotonic()
            return {
                "target_values_per_second": rate * nominal_points_per_batch,
                "scheduled_batches": batches,
                "published_builder_batches": published,
                "points_per_complete_row": len(point_types),
                "complete_rows_per_batch": records_per_batch,
                "published_values": published * records_per_batch * len(point_types),
                "publish_seconds": publish_end - started_at,
                "publish_batches_per_second": published / (publish_end - started_at),
                "tdengine_rows": last_count,
                "drain_seconds": drained_at - publish_end,
                "confirmed_values_per_second_end_to_end": (
                    last_count * len(point_types) / (drained_at - started_at)
                ),
                "fully_persisted": last_count == expected_rows,
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


_WIDE_POINT_COUNT = 500
_WIDE_DEVICE_COUNT_MAX = 300
_WIDE_IN_FLIGHT_LIMIT = 64
_WIDE_POINT_IDS = tuple(f"capacity_point_{index:03d}" for index in range(_WIDE_POINT_COUNT))


def _wide_devices() -> tuple[str, ...]:
    count = int(os.environ.get("STORAGE_WIDE_DEVICES", "30"))
    if not 1 <= count <= _WIDE_DEVICE_COUNT_MAX:
        raise ValueError(f"STORAGE_WIDE_DEVICES must be in [1, {_WIDE_DEVICE_COUNT_MAX}]")
    return tuple(f"CAP_WTG{index:03d}" for index in range(count))


@pytest.fixture
def storage_wide_capacity_config(harness: EightServiceHarness) -> tuple[str, ...]:
    """在发布前构造精确 500 点 schema，并禁止 Collector 采集容量设备副本。"""
    templates = harness.root / "configs" / "templates"
    point_table_path = templates / "point_tables" / "point_table_modbus_wtg_sim_v1.2.0.yaml"
    catalog_path = templates / "business_points_standard_v1.5.0.yaml"
    point_table = yaml.safe_load(point_table_path.read_text(encoding="utf-8"))
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))

    # 原 WTG 仍在采集和仿真，给它单独保留一份逐字段相同的 32 点 runtime
    # 绑定；500 点表只由 enabled:false 的容量设备型号引用。
    model_path = templates / "device_models" / "device_model_wtg_sim_physical_v1.2.0.yaml"
    original_model = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    runtime_table = copy.deepcopy(point_table)
    runtime_table["template_id"] = "modbus_wtg_sim_runtime"
    runtime_table_path = (
        templates / "point_tables" / "point_table_modbus_wtg_sim_runtime_v1.2.0.yaml"
    )
    runtime_table_path.write_text(yaml.safe_dump(runtime_table, sort_keys=False), encoding="utf-8")
    runtime_model = copy.deepcopy(original_model)
    runtime_model["point_table"]["id"] = "modbus_wtg_sim_runtime"
    model_path.write_text(yaml.safe_dump(runtime_model, sort_keys=False), encoding="utf-8")
    capacity_model = copy.deepcopy(original_model)
    capacity_model["template_id"] = "wtg_sim_capacity"
    capacity_model["name"] = "Storage 500 点容量风机（禁止采集）"
    capacity_model_path = templates / "device_models" / "device_model_wtg_sim_capacity_v1.2.0.yaml"
    capacity_model_path.write_text(
        yaml.safe_dump(capacity_model, sort_keys=False), encoding="utf-8"
    )

    points: dict[str, Any] = {}
    for index, point_id in enumerate(_WIDE_POINT_IDS):
        business_point_id = f"capacity_business_point_{index:03d}"
        points[point_id] = {
            "business_point_id": business_point_id,
            "point_groups": ["all", "measurement"],
            "address": {
                "register_type": "input",
                "address": index * 2,
                "count": 2,
                "slave_id": 1,
            },
            "data_type": "float32",
            "unit": "none",
            "scale": 1.0,
            "offset": 0.0,
            "description": f"Storage 500 点容量测点 {index:03d}",
        }
        catalog["business_points"][business_point_id] = {
            "data_type": "float32",
            "unit": "none",
            "description": f"Storage 500 点容量业务身份 {index:03d}",
        }
    point_table["points"] = points
    assert len(points) == _WIDE_POINT_COUNT
    assert all(point["data_type"] == "float32" for point in points.values())
    point_table_path.write_text(yaml.safe_dump(point_table, sort_keys=False), encoding="utf-8")
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")

    devices_path = harness.root / "configs" / "instances" / "device_e2e-final_v1.0.1.yaml"
    instances = yaml.safe_load(devices_path.read_text(encoding="utf-8"))
    source = instances["devices"][DEVICE]
    devices = _wide_devices()
    for device_id in devices:
        replica = copy.deepcopy(source)
        replica["name"] = f"Storage 容量设备 {device_id}（禁止采集）"
        replica["enabled"] = False
        replica["model"] = {"id": "wtg_sim_capacity", "version": "1.2.0"}
        instances["devices"][device_id] = replica
    devices_path.write_text(yaml.safe_dump(instances, sort_keys=False), encoding="utf-8")
    writer_count = int(os.environ.get("STORAGE_WIDE_WRITER_COUNT", "4"))
    if writer_count not in {1, 2, 4, 8}:
        raise ValueError("STORAGE_WIDE_WRITER_COUNT must be 1, 2, 4, or 8")
    storage_service_path = harness.root / "configs" / "services" / "service_storage-e2e_v2.0.0.yaml"
    storage_service = yaml.safe_load(storage_service_path.read_text(encoding="utf-8"))
    tdengine = storage_service["service"]["tdengine"]
    tdengine["ingestion"]["writer_count"] = writer_count
    tdengine["max_inflight_sql"] = max(tdengine["max_inflight_sql"], writer_count)
    tdengine["max_connections"] = max(tdengine["max_connections"], writer_count)
    storage_service_path.write_text(yaml.safe_dump(storage_service, sort_keys=False))
    return devices


@pytest.fixture
def storage_wide_started(
    storage_wide_capacity_config: tuple[str, ...],
    harness: EightServiceHarness,
) -> EightServiceHarness:
    """Kafka 直注容量只需真实 Configuration 和 Storage，不启动无关八服务。"""
    assert storage_wide_capacity_config
    harness.start("configuration")
    harness.wait_healthy("configuration")
    publish = harness.cli(
        "config",
        "publish",
        "--root",
        str(harness.root / "configs"),
        timeout=180,
    )
    assert publish["status"] == "SUCCESS", publish
    harness.env["WIND_HUB_CONFIGURATION_REVISION"] = str(int(publish["data"]["revision_id"]))
    asyncio.run(_ensure_topic(harness.stack.kafka, "windhub.e2e.history"))
    harness.start("storage")
    harness.wait_targets("storage")
    return harness


def _wide_value(point_index: int, cycle: int) -> float:
    return float(cycle * 1000 + point_index) + 0.25


def _nearest_rank_ms(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1] * 1000


def _storage_process_pid(harness: EightServiceHarness) -> int:
    payload = harness.status("storage")
    data = payload.get("data") or []
    item = data[0] if isinstance(data, list) else data
    process = item.get("process") or {}
    pid = process.get("pid")
    assert type(pid) is int and process.get("running") is True, payload
    return pid


def _read_process_sample(pid: int) -> tuple[int, int, int]:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat[stat.rindex(")") + 2 :].split()
    ticks = int(fields[11]) + int(fields[12])
    rss_kib = 0
    peak_rss_kib = 0
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            rss_kib = int(line.split()[1])
        elif line.startswith("VmHWM:"):
            peak_rss_kib = int(line.split()[1])
    return ticks, rss_kib, max(rss_kib, peak_rss_kib)


def _wide_counts_by_device(
    harness: EightServiceHarness,
    *,
    database: str,
    point_table_id: str,
    devices: tuple[str, ...],
    start_us: int,
    end_us: int,
) -> dict[str, int]:
    """用同一个 TDengine 半开窗口逐设备计数，不能以 topic offset 代替。"""
    selected = set(devices)
    counts = {device: 0 for device in devices}
    stables = harness.stack.tdengine_sql(f"SHOW `{database}`.STABLES", database=database)["data"]
    for stable, *_ in stables:
        rows = harness.stack.tdengine_sql(
            f"SELECT device_id, COUNT(*) FROM `{database}`.`{stable}` "
            f"WHERE point_table_id = '{point_table_id}' "
            f"AND ts >= {start_us} AND ts < {end_us} GROUP BY device_id",
            database=database,
            timeout=60,
        )["data"]
        for device_id, count, *_ in rows:
            if str(device_id) in selected:
                counts[str(device_id)] += int(count)
    return counts


async def _blocked_partitions(
    stub: storage_rpc.StorageServiceStub,
    metadata: tuple[tuple[str, str], ...],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    token = ""
    for _ in range(100):
        reply = await stub.ListBlockedPartitions(
            storage_pb.ListBlockedPartitionsRequest(page_size=1000, page_token=token),
            metadata=metadata,
            timeout=20,
        )
        items.extend(
            {
                "topic": item.topic,
                "partition": item.partition,
                "blocked_offset": item.blocked_offset,
                "reason": item.block_reason,
            }
            for item in reply.items
        )
        if reply.complete:
            assert not reply.next_page_token
            return items
        assert reply.next_page_token and reply.next_page_token != token
        token = reply.next_page_token
    raise AssertionError("ListBlockedPartitions pagination did not converge")


def _timestamp_from_us(value: int) -> Timestamp:
    moment = datetime.fromtimestamp(value // 1_000_000, UTC).replace(microsecond=value % 1_000_000)
    result = Timestamp()
    result.FromDatetime(moment)
    return result


async def _verify_wide_edge(
    stub: storage_rpc.StorageServiceStub,
    *,
    metadata: tuple[tuple[str, str], ...],
    device: str,
    point_table_id: str,
    moment_us: int,
    cycle: int,
) -> dict[str, object]:
    """单行查询后同时逐值验证 500 个字段及对应 GOOD 质量位。"""
    reply = await stub.QuerySamples(
        storage_pb.QuerySamplesRequest(
            filter=storage_pb.StorageFilter(
                wind_farm_id="e2e-final",
                device_ids=(device,),
                point_table_id=point_table_id,
                data_level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
                start_time=_timestamp_from_us(moment_us),
                end_time=_timestamp_from_us(moment_us + 1000),
            ),
            page_size=1,
        ),
        metadata=metadata,
        timeout=30,
    )
    assert reply.complete and not reply.next_page_token, reply
    assert len(reply.groups) == 1, reply
    group = reply.groups[0]
    assert group.device_id == device
    assert len(group.rows) == 1, group
    columns = tuple(column.point_id for column in group.columns)
    assert columns == _WIDE_POINT_IDS, (len(columns), columns[:3], columns[-3:])
    row = group.rows[0]
    assert row.sample_time == _timestamp_from_us(moment_us)
    assert len(row.values) == _WIDE_POINT_COUNT
    qualities = unpack_quality_bitmap(row.quality, _WIDE_POINT_COUNT)
    assert tuple(int(quality) for quality in qualities) == (1,) * _WIDE_POINT_COUNT
    for index, value in enumerate(row.values):
        assert value.WhichOneof("value") == "value_f64", (index, value)
        assert value.value_f64 == _wide_value(index, cycle), (index, value.value_f64)
    return {
        "device_id": device,
        "sample_time_us": moment_us,
        "cycle": cycle,
        "float32_values_verified_individually": len(row.values),
        "good_quality_values_verified_individually": len(qualities),
    }


def test_storage_single_instance_500_point_device_row_capacity(
    storage_wide_started: EightServiceHarness,
) -> None:
    """单 Storage 的 Kafka v3 完整 500 点设备行容量与持久化身份闭合。"""
    harness = storage_wide_started
    devices = _wide_devices()
    cycles = int(os.environ.get("STORAGE_WIDE_CYCLES", "600"))
    rate_per_device = int(os.environ.get("STORAGE_WIDE_RATE_PER_DEVICE", "10"))
    writer_count = int(os.environ.get("STORAGE_WIDE_WRITER_COUNT", "4"))
    drain_timeout = float(os.environ.get("STORAGE_WIDE_DRAIN_TIMEOUT_SECONDS", "900"))
    if cycles <= 0 or rate_per_device <= 0 or rate_per_device > 1000:
        raise ValueError("positive cycles and a rate in [1, 1000] are required")
    if 1000 % rate_per_device:
        raise ValueError("STORAGE_WIDE_RATE_PER_DEVICE must divide 1000 for millisecond timestamps")
    if drain_timeout <= 0:
        raise ValueError("positive drain timeout required")
    interval_us = 1_000_000 // rate_per_device
    point_table_id = "point_table_modbus_wtg_sim_v1.2.0"
    database = WideSchema.database_name("e2e-final")
    total_rows = len(devices) * cycles
    target_rows_per_second = len(devices) * rate_per_device
    producer_inflight = int(
        os.environ.get(
            "STORAGE_WIDE_PRODUCER_MAX_INFLIGHT",
            str(256 if target_rows_per_second > 1000 else _WIDE_IN_FLIGHT_LIMIT),
        )
    )
    if not 1 <= producer_inflight <= 512:
        raise ValueError("Storage capacity producer in-flight must be in [1, 512]")
    # Every device shares the same 500 physical values within a cycle. Build
    # these immutable JSON fragments before the timed producer phase instead
    # of reserializing 500 points for each of 300 devices.
    point_payloads = tuple(
        json.dumps(
            {
                point_id: [_wide_value(index, cycle), 1]
                for index, point_id in enumerate(_WIDE_POINT_IDS)
            },
            separators=(",", ":"),
        ).encode()
        for cycle in range(cycles)
    )
    result_dir = Path(os.environ.get("STORAGE_WIDE_RESULT_DIR", "/tmp"))
    result_dir.mkdir(parents=True, exist_ok=True)
    writer_count = int(os.environ.get("STORAGE_WIDE_WRITER_COUNT", "4"))
    stem = f"storage_wide_500_devices_{len(devices):03d}_w{writer_count}"
    result_path = result_dir / f"{stem}.json"
    jsonl_path = result_dir / f"{stem}.jsonl"

    async def exercise() -> dict[str, object]:
        producer = AIOKafkaProducer(
            bootstrap_servers=harness.stack.kafka,
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
            max_batch_size=1048576,
        )
        pid = await asyncio.to_thread(_storage_process_pid, harness)
        metadata = (("authorization", f"Bearer {harness.env['WIND_HUB_STORAGE_OPERATOR_TOKEN']}"),)
        progress: dict[str, object] = {
            "phase": "publish",
            "acknowledged_rows": 0,
            "published_payload_bytes": 0,
            "persisted_rows": 0,
            "persisted_devices": 0,
        }
        ack_latencies: list[float] = []
        process_samples: list[dict[str, object]] = []
        stop_monitor = asyncio.Event()
        pending: set[asyncio.Task[None]] = set()
        sample_start_us = (int(datetime.now(UTC).timestamp() * 1000) + 1000) * 1000
        sample_end_us = sample_start_us + cycles * interval_us
        counts = {device: 0 for device in devices}
        started_at = 0.0
        publish_end = 0.0
        drained_at = 0.0

        async def monitor(stream: Any) -> None:
            previous_at = time.monotonic()
            previous_ticks, rss_kib, peak_rss_kib = await asyncio.to_thread(
                _read_process_sample, pid
            )
            process_samples.append(
                {
                    "monotonic": previous_at,
                    "ticks": previous_ticks,
                    "rss_kib": rss_kib,
                    "peak_rss_kib": peak_rss_kib,
                }
            )
            hz = os.sysconf("SC_CLK_TCK")
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_monitor.wait(), timeout=1.0)
                now = time.monotonic()
                ticks, rss_kib, peak_rss_kib = await asyncio.to_thread(_read_process_sample, pid)
                cpu_percent = 100 * ((ticks - previous_ticks) / hz) / (now - previous_at)
                sample = {
                    "monotonic": now,
                    "ticks": ticks,
                    "rss_kib": rss_kib,
                    "peak_rss_kib": peak_rss_kib,
                    "cpu_percent_of_one_core": cpu_percent,
                }
                process_samples.append(sample)
                stream.write(
                    json.dumps(
                        {
                            "elapsed_seconds": now - started_at,
                            **progress,
                            "published_payload_MB": int(progress["published_payload_bytes"]) / 1e6,
                            "storage_cpu_percent_of_one_core_interval": cpu_percent,
                            "storage_rss_MiB": rss_kib / 1024,
                            "storage_peak_rss_MiB": peak_rss_kib / 1024,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                previous_at, previous_ticks = now, ticks
                if stop_monitor.is_set():
                    return

        async def publish_one(device: str, cycle: int, sample_time_us: int) -> None:
            sample_time = (
                datetime.fromtimestamp(sample_time_us // 1_000_000, UTC)
                .replace(microsecond=sample_time_us % 1_000_000)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            prefix = (
                b'{"schema_version":3,"wind_farm_id":"e2e-final",'
                b'"device_config_id":'
                + json.dumps(DEVICE_CONFIG_ID).encode()
                + b',"device_id":'
                + json.dumps(device).encode()
                + b',"data_level":"protocol","sample_time":'
                + json.dumps(sample_time).encode()
                + b',"points":'
            )
            encoded = prefix + point_payloads[cycle] + b"}"
            if cycle == 0 and device == devices[0]:
                # Validate the optimized producer against the same strict
                # physical-row decoder used by Storage's audit path.
                parsed = WideRecord.from_kafka_json(encoded, physical_points=_WIDE_POINT_IDS)
                assert parsed.device_id == device
                assert parsed.values == tuple(
                    _wide_value(index, cycle) for index in range(_WIDE_POINT_COUNT)
                )
            send_started = time.perf_counter()
            confirmation = await producer.send_and_wait(
                "windhub.e2e.history",
                key=f"e2e-final:{device}".encode(),
                value=encoded,
            )
            assert confirmation.topic == "windhub.e2e.history"
            ack_latencies.append(time.perf_counter() - send_started)
            progress["acknowledged_rows"] = int(progress["acknowledged_rows"]) + 1
            progress["published_payload_bytes"] = int(progress["published_payload_bytes"]) + len(
                encoded
            )

        with jsonl_path.open("w", encoding="utf-8", buffering=1) as stream:
            monitor_task: asyncio.Task[None] | None = None
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{_storage_port(harness)}")
            stub = storage_rpc.StorageServiceStub(channel)
            try:
                await producer.start()
                started_at = time.monotonic()
                monitor_task = asyncio.create_task(monitor(stream))
                next_send = started_at
                for sequence in range(total_rows):
                    device = devices[sequence % len(devices)]
                    cycle = sequence // len(devices)
                    sample_time_us = sample_start_us + cycle * interval_us
                    pending.add(asyncio.create_task(publish_one(device, cycle, sample_time_us)))
                    if len(pending) >= producer_inflight:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        await asyncio.gather(*done)
                    next_send += 1 / target_rows_per_second
                    await asyncio.sleep(max(0.0, next_send - time.monotonic()))
                if pending:
                    await asyncio.gather(*pending)
                    pending = set()
                publish_end = time.monotonic()
                assert not await _blocked_partitions(stub, metadata)

                progress["phase"] = "drain"
                deadline = time.monotonic() + drain_timeout
                while time.monotonic() < deadline:
                    try:
                        counts = await asyncio.to_thread(
                            _wide_counts_by_device,
                            harness,
                            database=database,
                            point_table_id=point_table_id,
                            devices=devices,
                            start_us=sample_start_us,
                            end_us=sample_end_us,
                        )
                    except R5StackError:
                        await asyncio.sleep(1)
                        continue
                    progress["persisted_rows"] = sum(counts.values())
                    progress["persisted_devices"] = sum(
                        count == cycles for count in counts.values()
                    )
                    assert not await _blocked_partitions(stub, metadata)
                    if all(count == cycles for count in counts.values()):
                        break
                    assert all(count <= cycles for count in counts.values()), counts
                    await asyncio.sleep(2)
                drained_at = time.monotonic()
                blocked = await _blocked_partitions(stub, metadata)
                progress["phase"] = "verification"
                complete = set(counts) == set(devices) and all(
                    count == cycles for count in counts.values()
                )
                if complete:
                    edge_verification = {
                        "first": await _verify_wide_edge(
                            stub,
                            metadata=metadata,
                            device=devices[0],
                            point_table_id=point_table_id,
                            moment_us=sample_start_us,
                            cycle=0,
                        ),
                        "last": await _verify_wide_edge(
                            stub,
                            metadata=metadata,
                            device=devices[0],
                            point_table_id=point_table_id,
                            moment_us=sample_end_us - interval_us,
                            cycle=cycles - 1,
                        ),
                    }
                else:
                    edge_verification = {
                        "status": "NOT_RUN",
                        "reason": "incomplete persistence prevents first/last-row qualification",
                    }
            finally:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await producer.stop()
                await channel.close()
                stop_monitor.set()
                if monitor_task is not None:
                    await monitor_task

        publish_seconds = publish_end - started_at
        end_to_end_seconds = drained_at - started_at
        published_rows = int(progress["acknowledged_rows"])
        published_bytes = int(progress["published_payload_bytes"])
        first_sample = process_samples[0]
        last_sample = process_samples[-1]
        process_seconds = float(last_sample["monotonic"]) - float(first_sample["monotonic"])
        average_cpu = (
            100
            * ((int(last_sample["ticks"]) - int(first_sample["ticks"])) / os.sysconf("SC_CLK_TCK"))
            / process_seconds
            if process_seconds > 0
            else 0.0
        )
        return {
            "test": "single Storage / Kafka v3 / 500-point complete device rows",
            "devices": list(devices),
            "device_count": len(devices),
            "cycles_per_device": cycles,
            "points_per_complete_row": _WIDE_POINT_COUNT,
            "rate_per_device_rows_per_second": rate_per_device,
            "writer_count": writer_count,
            "max_inflight_sql": max(4, writer_count),
            "max_connections": max(8, writer_count),
            "target_rows_per_second": target_rows_per_second,
            "expected_rows": total_rows,
            "expected_points": total_rows * _WIDE_POINT_COUNT,
            "sample_window": {
                "start_us_inclusive": sample_start_us,
                "end_us_exclusive": sample_end_us,
                "interval_us": interval_us,
            },
            "kafka": {
                "acks": "all",
                "enable_idempotence": True,
                "key": "wind_farm_id:device_id",
                "max_test_tasks_in_flight": producer_inflight,
                "acknowledged_rows": published_rows,
                "ack_latency_p95_ms": _nearest_rank_ms(ack_latencies, 0.95),
                "ack_latency_p99_ms": _nearest_rank_ms(ack_latencies, 0.99),
                "ack_latency_scope": (
                    "单条 send_and_wait 调用至 RecordMetadata 返回；包含本地 batching/linger "
                    "和 broker 副本 ACK，不含等待测试在途额度的时间，不是 TDengine E2E 写延迟"
                ),
                "ack_latency_percentile_method": "nearest-rank",
            },
            "published_rows_per_second": published_rows / publish_seconds,
            "published_points_per_second": published_rows * _WIDE_POINT_COUNT / publish_seconds,
            "published_payload_MB_per_second": published_bytes / 1e6 / publish_seconds,
            "publish_seconds": publish_seconds,
            "drain_seconds": drained_at - publish_end,
            "end_to_end_confirmed_rows_per_second": sum(counts.values()) / end_to_end_seconds,
            "end_to_end_confirmed_points_per_second": (
                sum(counts.values()) * _WIDE_POINT_COUNT / end_to_end_seconds
            ),
            "tdengine_counts_by_device_half_open_window": counts,
            "fully_persisted": set(counts) == set(devices)
            and all(count == cycles for count in counts.values()),
            "blocked_partitions": blocked,
            "dlq": {
                "applicable": False,
                "reason": "本用例直接注入 Kafka history，链路不涉及 NATS/JetStream 或其 DLQ",
            },
            "edge_verification": edge_verification,
            "storage_process": {
                "pid": pid,
                "average_cpu_percent_of_one_core": average_cpu,
                "peak_interval_cpu_percent_of_one_core": max(
                    (
                        float(sample.get("cpu_percent_of_one_core", 0.0))
                        for sample in process_samples
                    ),
                    default=0.0,
                ),
                "peak_rss_MiB": max(int(sample["peak_rss_kib"]) for sample in process_samples)
                / 1024,
                "sample_count": len(process_samples),
                "scope": (
                    "单个 Storage OS 进程；CPU=/proc/PID/stat utime+stime 增量/墙钟/单核，"
                    "RSS 峰值=/proc/PID/status VmHWM；不是整机或容器总 CPU"
                ),
            },
            "result_jsonl": str(jsonl_path),
        }

    result = asyncio.run(exercise())
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    kafka_result = result["kafka"]
    assert isinstance(kafka_result, dict)
    assert kafka_result["acknowledged_rows"] == total_rows, result
    assert result["published_rows_per_second"] >= target_rows_per_second * 0.95, result
    assert result["fully_persisted"], result
    assert result["end_to_end_confirmed_rows_per_second"] >= target_rows_per_second * 0.95, result
    assert result["blocked_partitions"] == [], result
