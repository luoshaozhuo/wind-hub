"""独立 Kafka 发布器的 500 点完整设备行 ACK 容量验收，不启动 Storage。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.wide_record import WideDataLevel, WideRecord
from tests.fixtures.services.r5_stack import R5Stack, down_stack, up_stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.performance,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.kafka,
]

_WIND_FARM_ID = "e2e-final"
_DEVICE_CONFIG_ID = "device_e2e-final_v1.0.1"
_TOPIC = "windhub.e2e.history"
_POINT_COUNT = 500
_DEVICE_COUNT = 300
_CYCLES = 600
_MINIMUM_ROWS_PER_SECOND = 2850.0
_POINT_IDS = tuple(f"capacity_point_{index:03d}" for index in range(_POINT_COUNT))
_DEVICES = tuple(f"CAP_WTG{index:03d}" for index in range(_DEVICE_COUNT))


def _wide_value(point_index: int, cycle: int) -> float:
    return float(cycle * 1000 + point_index) + 0.25


def _nearest_rank_ms(values: list[float], percentile: float) -> float:
    assert values
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1] * 1000


@pytest.fixture
async def kafka_stack() -> AsyncIterator[tuple[R5Stack, str]]:
    """每次档位使用独立 R5Stack 和唯一 topic，结束后销毁 Kafka 数据及整个栈。"""
    stack = up_stack()
    topic = f"{_TOPIC}.publisher.{uuid.uuid4().hex}"
    admin = AIOKafkaAdminClient(bootstrap_servers=stack.kafka)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
        yield stack, topic
    finally:
        try:
            await admin.delete_topics([topic])
        finally:
            await admin.close()
            down_stack(stack)


def _verify_sample(raw: bytes, *, device: str, cycle: int, sample_time_us: int) -> None:
    """严格解码抽检行，并逐点核对值、顺序和 500 个 GOOD 质量位。"""
    record = WideRecord.from_kafka_json(raw, physical_points=_POINT_IDS)
    assert record.wind_farm_id == _WIND_FARM_ID
    assert record.device_config_id == _DEVICE_CONFIG_ID
    assert record.device_id == device
    assert record.data_level == WideDataLevel.PROTOCOL
    assert record.sample_time_us == sample_time_us
    assert record.point_ids == _POINT_IDS
    assert record.values == tuple(_wide_value(index, cycle) for index in range(_POINT_COUNT))
    qualities = unpack_quality_bitmap(record.quality_bitmap, _POINT_COUNT)
    assert tuple(int(quality) for quality in qualities) == (1,) * _POINT_COUNT


@pytest.mark.asyncio
async def test_storage_capacity_publisher(kafka_stack: tuple[R5Stack, str]) -> None:
    """测量真实 broker 全副本幂等 ACK，不以入队完成或 Storage 持久化代替。"""
    stack, topic = kafka_stack
    max_inflight = int(os.environ.get("STORAGE_WIDE_PRODUCER_MAX_INFLIGHT", "64"))
    if max_inflight not in {64, 128, 256}:
        raise ValueError("STORAGE_WIDE_PRODUCER_MAX_INFLIGHT must be 64, 128, or 256")
    result_dir = Path(os.environ.get("STORAGE_WIDE_RESULT_DIR", "/tmp"))
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"publisher_{max_inflight}.json"

    # 与 Storage 容量用例相同：同周期的 500 点不可变 JSON 片段在计时前编码。
    point_payloads = tuple(
        json.dumps(
            {point_id: [_wide_value(index, cycle), 1] for index, point_id in enumerate(_POINT_IDS)},
            separators=(",", ":"),
        ).encode()
        for cycle in range(_CYCLES)
    )
    sample_start_us = (int(datetime.now(UTC).timestamp() * 1000) + 1000) * 1000
    producer = AIOKafkaProducer(
        bootstrap_servers=stack.kafka,
        acks="all",
        enable_idempotence=True,
        linger_ms=5,
        max_batch_size=1048576,
    )
    pending: set[asyncio.Task[None]] = set()
    ack_latencies: list[float] = []
    acknowledged_rows = 0
    published_payload_bytes = 0
    peak_pending = 0
    verified_samples = 0

    async def publish_one(device: str, cycle: int, raw: bytes) -> None:
        nonlocal acknowledged_rows, published_payload_bytes
        send_started = time.perf_counter()
        confirmation = await producer.send_and_wait(
            topic,
            key=f"{_WIND_FARM_ID}:{device}".encode(),
            value=raw,
        )
        assert confirmation.topic == topic
        ack_latencies.append(time.perf_counter() - send_started)
        acknowledged_rows += 1
        published_payload_bytes += len(raw)

    await producer.start()
    started_at = time.monotonic()
    try:
        for sequence in range(_DEVICE_COUNT * _CYCLES):
            device = _DEVICES[sequence % _DEVICE_COUNT]
            cycle = sequence // _DEVICE_COUNT
            sample_time_us = sample_start_us + cycle * 100_000
            sample_time = (
                datetime.fromtimestamp(sample_time_us // 1_000_000, UTC)
                .replace(microsecond=sample_time_us % 1_000_000)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            raw = (
                b'{"schema_version":3,"wind_farm_id":"e2e-final",'
                b'"device_config_id":'
                + json.dumps(_DEVICE_CONFIG_ID).encode()
                + b',"device_id":'
                + json.dumps(device).encode()
                + b',"data_level":"protocol","sample_time":'
                + json.dumps(sample_time).encode()
                + b',"points":'
                + point_payloads[cycle]
                + b"}"
            )
            if device in {_DEVICES[0], _DEVICES[-1]} and cycle in {0, _CYCLES - 1}:
                _verify_sample(raw, device=device, cycle=cycle, sample_time_us=sample_time_us)
                verified_samples += 1
            pending.add(asyncio.create_task(publish_one(device, cycle, raw)))
            peak_pending = max(peak_pending, len(pending))
            if len(pending) >= max_inflight:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                await asyncio.gather(*done)
        if pending:
            await asyncio.gather(*pending)
            pending = set()
    finally:
        # 失败时也要有界取消全部发送任务，再停止 producer 和销毁真实服务栈。
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await producer.stop()
    publish_seconds = time.monotonic() - started_at
    rows_per_second = acknowledged_rows / publish_seconds
    result = {
        "test": "independent Kafka publisher / 500-point complete device rows",
        "storage_started": False,
        "kafka": {
            "bootstrap_servers": stack.kafka,
            "topic": topic,
            "acks": "all",
            "enable_idempotence": True,
            "key": "wind_farm_id:device_id",
            "max_inflight": max_inflight,
            "peak_pending_tasks": peak_pending,
        },
        "device_count": _DEVICE_COUNT,
        "cycles_per_device": _CYCLES,
        "points_per_complete_row": _POINT_COUNT,
        "schema_version": 3,
        "device_config_id": _DEVICE_CONFIG_ID,
        "data_level": "protocol",
        "expected_rows": _DEVICE_COUNT * _CYCLES,
        "expected_points": _DEVICE_COUNT * _CYCLES * _POINT_COUNT,
        "acknowledged_rows": acknowledged_rows,
        "strict_samples_verified": verified_samples,
        "good_quality_values_verified_individually": verified_samples * _POINT_COUNT,
        "publish_seconds": publish_seconds,
        "acknowledged_rows_per_second": rows_per_second,
        "acknowledged_points_per_second": rows_per_second * _POINT_COUNT,
        "published_payload_MB_per_second": published_payload_bytes / 1e6 / publish_seconds,
        "published_payload_MB": published_payload_bytes / 1e6,
        "ack_latency_p95_ms": _nearest_rank_ms(ack_latencies, 0.95),
        "ack_latency_p99_ms": _nearest_rank_ms(ack_latencies, 0.99),
        "ack_latency_percentile_method": "nearest-rank",
        "minimum_rows_per_second": _MINIMUM_ROWS_PER_SECOND,
        "pass": rows_per_second >= _MINIMUM_ROWS_PER_SECOND,
    }
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    assert acknowledged_rows == _DEVICE_COUNT * _CYCLES, result
    assert peak_pending <= max_inflight, result
    assert verified_samples == 4, result
    assert rows_per_second >= _MINIMUM_ROWS_PER_SECOND, result
