"""R14（v1_48）单实例 600s 持续容量验收（S4 长稳口径）。

与 R13 同环境、同权威口径对齐：
- 不使用 cProfile；证据全部来自已有生产指标及进程边界之外——
  Kafka 端 offset（Broker 确认真值）、JetStream ConsumerInfo（服务端
  积压/重投真值）、/proc（CPU/RSS）、gRPC GetMetrics（事件环 P99、
  写延迟 P99）与 gRPC 可用性探针；
- 固定 500 点/批，开环令牌桶按全局目标批速率持续发布 R14_DURATION_S
  秒（默认 600s），发布结束后排空到零积压；
- 裁决以 Kafka 真实目标确认吞吐与「零丢失、DLQ=0、积压斜率≈0」为准，
  不以 Producer 发布速率代替；
- 关键参数走环境变量，便于先短烟测再正式长稳：
  R14_DURATION_S（默认 600）、R14_DEGREE（默认 2）、
  R14_TARGET_BATCH_RATE（全局批/s，默认 120）、R14_POINTS（默认 500）、
  R14_PUBLISH_ENCODERS（独立编码进程数，默认 4）；
  R14_MIN_SUSTAINED_FRACTION（默认 0.95）与
  R14_MAX_BACKLOG_SLOPE_FRACTION（默认 0.02）用于稳态容量裁决。
- JSON 结论与每秒取样 JSONL 保存在 R14_RESULT_DIR（默认 /tmp），
  文件名包含唯一测试实例标识，运行异常仍保留先前已冲刷的采样证据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import nats
import pytest
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaError

from contracts import common_pb2, common_pb2_grpc
from contracts.sample_batch import SampleBatch
from storage_service.domain.wide_record import WideRecord
from tests.sink_service.system.conftest import (
    SinkSystemHarness,
    make_system_batch,
    worker_pid_set,
)
from tests.sink_service.system.test_concurrent_delivery_e2e import (
    _consumer_infos,
    _delivery_id,
    _durable_name,
    _write_concurrent_root,
)
from tests.sink_service.system.test_concurrent_throughput_s4 import (
    CLK_TCK,
    _kafka_total_offset,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.load,
    pytest.mark.slow,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.soak,
    pytest.mark.nats,
    pytest.mark.jetstream,
    pytest.mark.kafka,
]

DURATION_S = float(os.environ.get("R14_DURATION_S", "600"))
DEGREE = int(os.environ.get("R14_DEGREE", "2"))
TARGET_BATCH_RATE = float(os.environ.get("R14_TARGET_BATCH_RATE", "120"))
POINTS_PER_BATCH = int(os.environ.get("R14_POINTS", "500"))
KAFKA_MAX_SAFE_INFLIGHT = int(os.environ.get("R14_KAFKA_MAX_SAFE_INFLIGHT", "4"))
KAFKA_MAX_BATCH_SIZE = int(os.environ.get("R14_KAFKA_MAX_BATCH_SIZE", "131072"))
KAFKA_LINGER_MS = int(os.environ.get("R14_KAFKA_LINGER_MS", "0"))
KAFKA_PARTITIONS = int(os.environ.get("R14_KAFKA_PARTITIONS", "1"))
PUBLISH_ENCODERS = int(os.environ.get("R14_PUBLISH_ENCODERS", "4"))
MIN_SUSTAINED_FRACTION = float(os.environ.get("R14_MIN_SUSTAINED_FRACTION", "0.95"))
MAX_BACKLOG_SLOPE_FRACTION = float(os.environ.get("R14_MAX_BACKLOG_SLOPE_FRACTION", "0.02"))
DRAIN_TIMEOUT_S = 120.0
SAMPLE_INTERVAL_S = 1.0
RELIABLE_STAGES = (
    "reliable_source_fetch",
    "reliable_encode",
    "reliable_write",
    "reliable_source_ack",
)
KAFKA_STAGES = ("lock", "metadata", "submit", "confirm")
QUOTA_STAGES = ("global", "physical", "native")
# 系统夹具默认 history stream 仅 256MiB、max_age=7200s（供有界 S4 工况）。
# JetStream Limits 保留策略下 ACK 只推进消费者位置、不删除流消息，600s 持续
# 压测累积约 7 万批会按 max_bytes 撑爆（与消费快慢无关）。长稳专用：
# - 放大 max_bytes 到 R14_STREAM_MAX_BYTES（默认 32GiB）；
# - 加滚动 max_age=R14_STREAM_MAX_AGE_S（默认 900s），覆盖 600s 发布、120s
#   排空及审计余量，让已消费的老消息到期
#   回收，使流占用与运行时长解耦。消费者跟得上时（积压斜率≈0）到期消息早已
#   安全交付；name/subject/存储/retention/discard 均不变，仍满足生产启动守卫
#   （reliable_runtime 仅要求 limits + 有限 max_bytes/max_age，不限定值）。
STREAM_MAX_BYTES = int(os.environ.get("R14_STREAM_MAX_BYTES", str(32 * 1024**3)))
STREAM_MAX_AGE_S = int(os.environ.get("R14_STREAM_MAX_AGE_S", "900"))


def _encode_r14_batch(
    token: str, seq: int, device: str, points: int, now: datetime
) -> tuple[str, bytes]:
    """Build and encode a strict fixture outside the publisher's GIL."""
    batch = make_system_batch("reliable", seq, points, token=token, device_id=device, now=now)
    return batch.subject(), batch.encode()


async def _kafka_partition_offsets(consumer: AIOKafkaConsumer, topic: str) -> dict[int, int]:
    partitions = consumer.partitions_for_topic(topic)
    attempts = 0
    while partitions is None and attempts < 5:
        await consumer._client.force_metadata_update()  # noqa: SLF001
        partitions = consumer.partitions_for_topic(topic)
        attempts += 1
    if not partitions:
        return {}
    tps = [TopicPartition(topic, partition) for partition in sorted(partitions)]
    for attempt in range(5):
        try:
            ends = await consumer.end_offsets(tps)
            return {tp.partition: ends[tp] for tp in tps}
        except KafkaError:
            if attempt == 4:
                raise
            await consumer._client.force_metadata_update()  # noqa: SLF001
            await asyncio.sleep(1)
    raise AssertionError("unreachable Kafka offset retry state")


def _devices(degree: int) -> list[str]:
    return [f"WT{i:03d}" for i in range(1, degree + 1)]


async def _grpc_probe(harness: SinkSystemHarness) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        async with harness.channel() as channel:
            status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
                common_pb2.Empty(), timeout=3
            )
        return status.health == "HEALTHY", (time.perf_counter() - start) * 1000
    except Exception:  # noqa: BLE001 - 可用性探针需要全量捕获
        return False, (time.perf_counter() - start) * 1000


async def _fetch_metrics(harness: SinkSystemHarness) -> dict[str, float]:
    try:
        async with harness.channel() as channel:
            response = await common_pb2_grpc.CommonServiceStub(channel).GetMetrics(
                common_pb2.GetMetricsRequest(window_ms=60000), timeout=5
            )
        return {metric.name: metric.value for metric in response.metrics}
    except Exception:  # noqa: BLE001 - 采样失败不得终止压测
        return {}


def _proc_cpu_rss(pids: set[int]) -> tuple[int, int]:
    ticks = 0
    rss_kb = 0
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            after = stat[stat.rindex(")") + 2 :].split()
            ticks += int(after[11]) + int(after[12])
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb += int(line.split()[1])
        except (FileNotFoundError, ProcessLookupError):
            continue
    return ticks, rss_kb


def _decode_capacity_row(payload: bytes) -> WideRecord:
    """Reject partial/legacy records before using offsets as wide-row evidence."""
    return WideRecord.from_kafka_json(
        payload, physical_points=tuple(f"p{i:03d}" for i in range(POINTS_PER_BATCH))
    )


async def _audit_duplicates(
    harness: SinkSystemHarness, total_values: int, baseline_offsets: dict[int, int]
) -> dict[str, Any]:
    """全量读 topic，按设备、等级、采样时间审计完整设备行重复身份。"""
    audit = AIOKafkaConsumer(
        harness.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"qa-r14-audit-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    await audit.start()
    stop_error = ""
    try:
        records: list[Any] = []
        deadline = time.monotonic() + 300
        while (
            time.monotonic() < deadline and len(records) < total_values // POINTS_PER_BATCH + 10000
        ):
            batch = await audit.getmany(timeout_ms=2000, max_records=1000)
            for messages in batch.values():
                records.extend(messages)
            if not batch:
                break
        identities: dict[tuple[str, str, str, int], int] = {}
        payload_bytes = 0
        scanned_records = 0
        for record in records:
            scanned_records += 1
            if record.offset < baseline_offsets.get(record.partition, 0):
                continue
            payload_bytes += len(record.value)
            row = _decode_capacity_row(record.value)
            key = (row.wind_farm_id, row.device_id, row.data_level.value, row.sample_time_us)
            identities[key] = identities.get(key, 0) + 1
        dupes = {key: count for key, count in identities.items() if count > 1}
        result = {
            "audit_baseline_offsets": baseline_offsets,
            "audit_scanned_records": scanned_records,
            "audit_payload_bytes": payload_bytes,
            "audit_total_records": len(identities)
            + sum(count - 1 for count in identities.values()),
            "audit_distinct_identities": len(identities),
            "audit_duplicate_identities": len(dupes),
            "audit_duplicate_record_excess": sum(c - 1 for c in dupes.values()),
            "audit_max_occurrence": max(dupes.values(), default=1),
            "audit_stop_error": stop_error,
        }
    finally:
        try:
            await asyncio.wait_for(audit.stop(), timeout=30)
        except (KafkaError, TimeoutError) as exc:
            stop_error = type(exc).__name__
            result["audit_stop_error"] = stop_error
    return result


async def _audit_device_order_sample(harness: SinkSystemHarness) -> dict[str, Any]:
    """Sample early p000 records and verify per-device sequence and partition stability."""
    audit = AIOKafkaConsumer(
        harness.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"qa-r14-order-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    await audit.start()
    try:
        observed: dict[str, list[tuple[int, int]]] = {}
        scanned = 0
        deadline = time.monotonic() + 60
        while scanned < 200000 and time.monotonic() < deadline:
            batch = await audit.getmany(timeout_ms=2000, max_records=5000)
            if not batch:
                break
            for messages in batch.values():
                for record in messages:
                    scanned += 1
                    row = _decode_capacity_row(record.value)
                    seq = int(row.values[0])
                    assert row.values == tuple(float(seq + i) for i in range(POINTS_PER_BATCH))
                    assert row.quality_bitmap == bytes([0x55] * ((POINTS_PER_BATCH + 3) // 4))
                    observed.setdefault(row.device_id, []).append((seq, record.partition))
            if len(observed) == DEGREE and all(len(items) >= 3 for items in observed.values()):
                break
        violations = {
            device: items
            for device, items in observed.items()
            if len({partition for _seq, partition in items}) != 1
            or any(a[0] >= b[0] for a, b in zip(items, items[1:], strict=False))
        }
        result = {
            "scanned_records": scanned,
            "devices_observed": len(observed),
            "minimum_sequences_per_device": min(
                (len(items) for items in observed.values()), default=0
            ),
            "device_partitions": {
                device: sorted({partition for _seq, partition in items})
                for device, items in observed.items()
            },
            "order_or_partition_violations": violations,
            "order_stop_error": "",
        }
    finally:
        try:
            await asyncio.wait_for(audit.stop(), timeout=30)
        except (KafkaError, TimeoutError) as exc:
            result["order_stop_error"] = type(exc).__name__
    return result


class TestR14SustainedCapacity:
    async def test_sustained_capacity_600s(
        self,
        sink_harness: SinkSystemHarness,
    ) -> None:
        if not 0 < MIN_SUSTAINED_FRACTION <= 1:
            raise ValueError("R14_MIN_SUSTAINED_FRACTION must be in (0, 1]")
        if not 0 <= MAX_BACKLOG_SLOPE_FRACTION < 1:
            raise ValueError("R14_MAX_BACKLOG_SLOPE_FRACTION must be in [0, 1)")
        if (
            not 1 <= DEGREE <= 1000
            or POINTS_PER_BATCH < 1
            or TARGET_BATCH_RATE <= 0
            or not 30 <= DURATION_S <= 86400
            or not 1 <= KAFKA_MAX_SAFE_INFLIGHT <= 128
            or not 1 <= KAFKA_MAX_BATCH_SIZE <= 1048576
            or not 0 <= KAFKA_LINGER_MS <= 10
            or not 1 <= KAFKA_PARTITIONS <= 32
            or not 1 <= PUBLISH_ENCODERS <= 16
        ):
            raise ValueError("invalid sustained load shape or duration")
        devices = _devices(DEGREE)
        delivery_ids = [_delivery_id(i) for i in range(DEGREE)]
        _write_concurrent_root(
            sink_harness,
            devices=devices,
            delivery_ids=delivery_ids,
            kafka_max_safe_inflight=KAFKA_MAX_SAFE_INFLIGHT,
            kafka_max_batch_size=KAFKA_MAX_BATCH_SIZE,
            kafka_linger_ms=KAFKA_LINGER_MS,
        )
        await sink_harness.provision_streams()
        # Explicitly create even the single-partition baseline so metadata
        # checks cannot race Kafka's automatic topic creation.
        admin = AIOKafkaAdminClient(bootstrap_servers=sink_harness.stack.kafka)
        await admin.start()
        try:
            await admin.create_topics(
                [
                    NewTopic(
                        name=sink_harness.kafka_topic,
                        num_partitions=KAFKA_PARTITIONS,
                        replication_factor=1,
                    )
                ]
            )
        finally:
            await admin.close()
        await self._enlarge_history_stream(sink_harness)
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, did) for did in delivery_ids}
        evidence_root = Path(os.environ.get("R14_RESULT_DIR", "/tmp")).resolve()
        if not evidence_root.is_dir() or evidence_root.is_symlink():
            raise ValueError("R14_RESULT_DIR must be an existing non-symlink directory")
        stem = (
            f"r14_capacity_d{DEGREE}_r{int(TARGET_BATCH_RATE)}_"
            f"c{KAFKA_MAX_SAFE_INFLIGHT}_b{KAFKA_MAX_BATCH_SIZE}_"
            f"l{KAFKA_LINGER_MS}_p{KAFKA_PARTITIONS}_{sink_harness.token}"
        )
        result_path = evidence_root / f"{stem}.json"
        samples_path = evidence_root / f"{stem}_samples.jsonl"
        try:
            await sink_harness.start()
            for did in delivery_ids:
                await self._wait_running(sink_harness, did)

            publisher = await nats.connect(servers=sink_harness.stack.nats, connect_timeout=10)
            consumer = AIOKafkaConsumer(
                bootstrap_servers=sink_harness.stack.kafka,
                client_id=f"qa-r14-cap-{sink_harness.token}",
            )
            pub_task: asyncio.Task[None] | None = None
            encoder_pool: ProcessPoolExecutor | None = None
            try:
                await consumer.start()
                baseline = await _kafka_total_offset(consumer, sink_harness.kafka_topic)
                baseline_partitions = await _kafka_partition_offsets(
                    consumer, sink_harness.kafka_topic
                )
                if len(baseline_partitions) != KAFKA_PARTITIONS:
                    raise AssertionError(
                        f"Kafka topic partition count: {len(baseline_partitions)} "
                        f"!= configured {KAFKA_PARTITIONS}"
                    )
                js = publisher.jetstream()
                interval = 1.0 / TARGET_BATCH_RATE
                loop = asyncio.get_running_loop()
                pool = ProcessPoolExecutor(
                    max_workers=PUBLISH_ENCODERS, mp_context=get_context("spawn")
                )
                encoder_pool = pool
                # Warm the isolated encoders before timing and verify their wire
                # output with the same strict contract used by production Sink.
                warm_time = datetime.now(UTC)
                warm = await asyncio.gather(
                    *(
                        loop.run_in_executor(
                            pool,
                            _encode_r14_batch,
                            sink_harness.token,
                            -index - 1,
                            devices[index % DEGREE],
                            POINTS_PER_BATCH,
                            warm_time,
                        )
                        for index in range(PUBLISH_ENCODERS)
                    )
                )
                for index, (subject, payload) in enumerate(warm):
                    expected = make_system_batch(
                        "reliable",
                        -index - 1,
                        POINTS_PER_BATCH,
                        token=sink_harness.token,
                        device_id=devices[index % DEGREE],
                        now=warm_time,
                    )
                    assert SampleBatch.decode(payload, "reliable", subject=subject) == expected

                published_batches = 0
                pub_seq = 0
                puback_seconds_total = 0.0
                pub_finished_at = 0.0
                max_pending = max(64, DEGREE * 8)
                scheduled_target = int(TARGET_BATCH_RATE * DURATION_S)

                async def _publish_loop() -> None:
                    nonlocal published_batches, pub_seq, puback_seconds_total, pub_finished_at
                    pending: deque[asyncio.Task[None]] = deque()
                    last_per_device: dict[str, asyncio.Task[None]] = {}

                    async def publish_ordered(
                        prior: asyncio.Task[None] | None,
                        seq: int,
                        device: str,
                        sample_time: datetime,
                    ) -> None:
                        nonlocal published_batches, puback_seconds_total
                        subject, payload = await loop.run_in_executor(
                            pool,
                            _encode_r14_batch,
                            sink_harness.token,
                            seq,
                            device,
                            POINTS_PER_BATCH,
                            sample_time,
                        )
                        if prior is not None:
                            await prior
                        # Count only a real server PubAck, not a queued local send.
                        puback_started = time.monotonic()
                        await js.publish(subject, payload, timeout=10)
                        puback_seconds_total += time.monotonic() - puback_started
                        published_batches += 1

                    try:
                        while pub_seq < scheduled_target:
                            now = time.monotonic()
                            if now >= stop_at:
                                break
                            due = t0 + pub_seq * interval
                            if due > now:
                                await asyncio.sleep(due - now)
                                continue
                            device = devices[pub_seq % DEGREE]
                            task = asyncio.create_task(
                                publish_ordered(
                                    last_per_device.get(device),
                                    pub_seq,
                                    device,
                                    datetime.now(UTC),
                                )
                            )
                            last_per_device[device] = task
                            pending.append(task)
                            pub_seq += 1
                            # Absolute deadlines avoid accumulating sleep rounding
                            # drift. The bounded in-flight window limits catch-up.
                            if len(pending) >= max_pending:
                                await pending.popleft()
                            elif pub_seq % 16 == 0:
                                await asyncio.sleep(0)
                        while pending:
                            await pending.popleft()
                        pub_finished_at = time.monotonic()
                    finally:
                        for task in pending:
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)

                samples: list[dict[str, Any]] = []
                main_pid = sink_harness.process.proc.pid  # type: ignore[union-attr]
                t0 = time.monotonic()
                stop_at = t0 + DURATION_S
                pub_task = asyncio.create_task(_publish_loop())

                # 持续期 1s 采样。
                while not pub_task.done():
                    pids = worker_pid_set(main_pid) | {main_pid}
                    ticks, rss_kb = _proc_cpu_rss(pids)
                    infos = await _consumer_infos(sink_harness)
                    per_pending = {
                        name: (
                            infos[name].num_pending + infos[name].num_ack_pending
                            if name in infos
                            else None
                        )
                        for name in names
                    }
                    redelivered = {
                        name: infos[name].num_redelivered if name in infos else None
                        for name in names
                    }
                    ok, grpc_ms = await _grpc_probe(sink_harness)
                    metrics = await _fetch_metrics(sink_harness)
                    sample = {
                        "t": time.monotonic(),
                        "publisher_puback_batches": published_batches,
                        "kafka_offset": await _kafka_total_offset(
                            consumer, sink_harness.kafka_topic
                        ),
                        "per_durable_pending": per_pending,
                        "redelivered": redelivered,
                        "cpu_ticks": ticks,
                        "rss_kb": rss_kb,
                        "grpc_ok": ok,
                        "grpc_ms": grpc_ms,
                        "metrics_ok": bool(metrics),
                        "lag_p99_ms": metrics.get("reliable_event_loop_lag_ms_p99"),
                        "write_latency_p99_ms": metrics.get("write_latency_ms_max_delivery_p99"),
                        "quota_timed_writes_total": metrics.get("quota_timed_writes_total"),
                        "quota_stages": {
                            stage: metrics.get(f"quota_{stage}_seconds_total")
                            for stage in QUOTA_STAGES
                        },
                        "kafka_successful_subbatches_total": metrics.get("write_success_total"),
                        "kafka_timed_writes_total": metrics.get("kafka_timed_writes_total"),
                        "kafka_stages": {
                            stage: metrics.get(f"kafka_{stage}_seconds_total")
                            for stage in KAFKA_STAGES
                        },
                        "reliable_stages": {
                            stage: {
                                "seconds_total": metrics.get(f"{stage}_seconds_total"),
                                "operations_total": metrics.get(f"{stage}_operations_total"),
                            }
                            for stage in RELIABLE_STAGES
                        },
                    }
                    samples.append(sample)
                    if len(samples) == 1:
                        (evidence_root / f"{stem}_metric_names.json").write_text(
                            json.dumps(sorted(metrics), indent=2), encoding="utf-8"
                        )
                    with samples_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(sample, separators=(",", ":")) + "\n")
                        stream.flush()
                    await asyncio.sleep(SAMPLE_INTERVAL_S)
                await pub_task
                t_pub_end = pub_finished_at

                # 排空期：等到全部 durable 零积压或超时。
                drain_deadline = time.monotonic() + DRAIN_TIMEOUT_S
                while time.monotonic() < drain_deadline:
                    infos = await _consumer_infos(sink_harness)
                    pending = sum(
                        infos[name].num_pending + infos[name].num_ack_pending
                        for name in names
                        if name in infos
                    )
                    if pending == 0:
                        break
                    await asyncio.sleep(0.5)
                # 终局前受控停止生产写入，防止顺序审计期间的新确认让
                # final_offset、final_pending 与全量身份审计互相错位。
                await sink_harness.stop()
                await consumer._client.force_metadata_update()  # noqa: SLF001
                final_infos = await _consumer_infos(sink_harness)
                final_pending = {
                    name: (
                        final_infos[name].num_pending + final_infos[name].num_ack_pending
                        if name in final_infos
                        else None
                    )
                    for name in names
                }
                final_offset = await _kafka_total_offset(consumer, sink_harness.kafka_topic)
                final_partitions = await _kafka_partition_offsets(
                    consumer, sink_harness.kafka_topic
                )
                t_end = time.monotonic()

                published_values = published_batches * POINTS_PER_BATCH
                confirmed_values = (final_offset - baseline) * POINTS_PER_BATCH
                wall_pub = max(t_pub_end - t0, 1e-6)

                # 稳态窗口：持续期 10%→90%，排除预热与排空。
                offsets = [
                    (s["t"], (s["kafka_offset"] - baseline) * POINTS_PER_BATCH)
                    for s in samples
                    if t0 + 0.1 * DURATION_S <= s["t"] <= t0 + 0.9 * DURATION_S
                ]
                if len(offsets) >= 2:
                    dt = offsets[-1][0] - offsets[0][0]
                    steady_values_s = (offsets[-1][1] - offsets[0][1]) / dt if dt > 0 else 0.0
                else:
                    steady_values_s = 0.0

                puback_series = [
                    (s["t"], s["publisher_puback_batches"])
                    for s in samples
                    if t0 + 0.1 * DURATION_S <= s["t"] <= t0 + 0.9 * DURATION_S
                ]
                steady_puback_batches_s = 0.0
                if len(puback_series) >= 2:
                    puback_dt = puback_series[-1][0] - puback_series[0][0]
                    if puback_dt > 0:
                        steady_puback_batches_s = (
                            puback_series[-1][1] - puback_series[0][1]
                        ) / puback_dt

                # 积压斜率（聚合）：稳态窗口末段 pending 线性趋势，应≈0。
                pend_series = [
                    (s["t"], sum(v for v in s["per_durable_pending"].values() if v is not None))
                    for s in samples
                    if t0 + 0.1 * DURATION_S <= s["t"] <= t0 + 0.9 * DURATION_S
                ]
                backlog_slope = 0.0
                if len(pend_series) >= 2:
                    dtp = pend_series[-1][0] - pend_series[0][0]
                    backlog_slope = (
                        (pend_series[-1][1] - pend_series[0][1]) / dtp if dtp > 0 else 0.0
                    )

                first, last = samples[0], samples[-1]
                stage_diagnostics: dict[str, dict[str, float | int | None]] = {}
                for stage in RELIABLE_STAGES:
                    valid = [
                        sample["reliable_stages"][stage]
                        for sample in samples
                        if all(
                            sample["reliable_stages"][stage][field] is not None
                            for field in ("seconds_total", "operations_total")
                        )
                    ]
                    if len(valid) < 2:
                        stage_diagnostics[stage] = {
                            "duration_seconds": None,
                            "operations": None,
                            "mean_duration_ms": None,
                        }
                        continue
                    before, after = valid[0], valid[-1]
                    duration = max(0.0, after["seconds_total"] - before["seconds_total"])
                    operations = max(0, int(after["operations_total"] - before["operations_total"]))
                    stage_diagnostics[stage] = {
                        "duration_seconds": round(duration, 3),
                        "operations": operations,
                        "mean_duration_ms": round(1000 * duration / operations, 3)
                        if operations
                        else None,
                    }
                kafka_diagnostics: dict[str, dict[str, float | int | None]] = {}
                for stage in KAFKA_STAGES:
                    valid = [
                        (sample["kafka_stages"][stage], sample["kafka_timed_writes_total"])
                        for sample in samples
                        if sample["kafka_stages"][stage] is not None
                        and sample["kafka_timed_writes_total"] is not None
                    ]
                    if len(valid) < 2:
                        kafka_diagnostics[stage] = {
                            "duration_seconds": None,
                            "writes": None,
                            "mean_duration_ms": None,
                        }
                        continue
                    before, after = valid[0], valid[-1]
                    duration = max(0.0, after[0] - before[0])
                    writes = max(0, int(after[1] - before[1]))
                    kafka_diagnostics[stage] = {
                        "duration_seconds": round(duration, 3),
                        "writes": writes,
                        "mean_duration_ms": round(duration * 1000 / writes, 3) if writes else None,
                    }
                quota_diagnostics: dict[str, dict[str, float | int | None]] = {}
                for stage in QUOTA_STAGES:
                    valid = [
                        (sample["quota_stages"][stage], sample["quota_timed_writes_total"])
                        for sample in samples
                        if sample["quota_stages"][stage] is not None
                        and sample["quota_timed_writes_total"] is not None
                    ]
                    if len(valid) < 2:
                        quota_diagnostics[stage] = {
                            "duration_seconds": None,
                            "writes": None,
                            "mean_duration_ms": None,
                        }
                        continue
                    before, after = valid[0], valid[-1]
                    duration = max(0.0, after[0] - before[0])
                    writes = max(0, int(after[1] - before[1]))
                    quota_diagnostics[stage] = {
                        "duration_seconds": round(duration, 3),
                        "writes": writes,
                        "mean_duration_ms": round(duration * 1000 / writes, 3) if writes else None,
                    }
                subbatch_samples = [
                    (
                        sample["kafka_successful_subbatches_total"],
                        sample["kafka_timed_writes_total"],
                    )
                    for sample in samples
                    if sample["kafka_successful_subbatches_total"] is not None
                    and sample["kafka_timed_writes_total"] is not None
                ]
                confirmed_subbatches_per_write = None
                if len(subbatch_samples) >= 2:
                    a, b = subbatch_samples[0], subbatch_samples[-1]
                    writes = b[1] - a[1]
                    if writes > 0:
                        confirmed_subbatches_per_write = round((b[0] - a[0]) / writes, 3)
                cpu_seconds = (last["cpu_ticks"] - first["cpu_ticks"]) / CLK_TCK
                grpc_failures = sum(1 for s in samples if not s["grpc_ok"])
                metrics_failures = sum(1 for s in samples if not s["metrics_ok"])
                grpc_max_ms = max(s["grpc_ms"] for s in samples)
                grpc_ordered = sorted(s["grpc_ms"] for s in samples)
                grpc_p95_ms = grpc_ordered[int((len(grpc_ordered) - 1) * 0.95)]
                grpc_p99_ms = grpc_ordered[int((len(grpc_ordered) - 1) * 0.99)]
                lag_p99_max = max(
                    (s["lag_p99_ms"] for s in samples if s["lag_p99_ms"] is not None),
                    default=None,
                )
                write_p99_max = max(
                    (
                        s["write_latency_p99_ms"]
                        for s in samples
                        if s["write_latency_p99_ms"] is not None
                    ),
                    default=None,
                )
                rss_max_mib = max(s["rss_kb"] for s in samples) / 1024
                redeliver_peak = {
                    name: peak
                    for name, peak in (
                        (n, max((s["redelivered"].get(n) or 0) for s in samples)) for n in names
                    )
                    if peak
                }
                dlq = await sink_harness.dlq_count()
                order_audit = await _audit_device_order_sample(sink_harness)

                result = {
                    "scenario": "r14_sustained_reliable",
                    "history_stream_max_bytes": STREAM_MAX_BYTES,
                    "history_stream_max_age_s": STREAM_MAX_AGE_S,
                    "duration_s": DURATION_S,
                    "degree": DEGREE,
                    "target_batch_rate_global": TARGET_BATCH_RATE,
                    "points_per_batch": POINTS_PER_BATCH,
                    "scheduled_target_batches": scheduled_target,
                    "scheduled_batches": pub_seq,
                    "published_batches": published_batches,
                    "published_values": published_values,
                    "confirmed_values": confirmed_values,
                    "physical_builder_count": last["kafka_successful_subbatches_total"],
                    "confirmed_points_per_physical_builder": round(
                        confirmed_values / last["kafka_successful_subbatches_total"], 3
                    )
                    if last["kafka_successful_subbatches_total"]
                    else None,
                    "cross_device_coalescing_observed": bool(
                        last["kafka_successful_subbatches_total"]
                        and confirmed_values / last["kafka_successful_subbatches_total"]
                        > POINTS_PER_BATCH
                    ),
                    "kafka_partition_count": len(final_partitions),
                    "kafka_confirmed_by_partition": {
                        str(partition): final_partitions.get(partition, 0)
                        - baseline_partitions.get(partition, 0)
                        for partition in sorted(final_partitions)
                    },
                    "device_order_audit": order_audit,
                    "zero_loss": confirmed_values == published_values,
                    "publish_wall_seconds": round(wall_pub, 3),
                    "achieved_publish_batches_per_s": round(published_batches / wall_pub, 2),
                    "publisher_puback_batches_per_s_steady": round(steady_puback_batches_s, 2),
                    "publisher_encoder_processes": PUBLISH_ENCODERS,
                    "publisher_max_inflight_batches": max_pending,
                    "publisher_puback_mean_ms": round(
                        1000 * puback_seconds_total / published_batches, 3
                    )
                    if published_batches
                    else None,
                    "confirmed_values_per_s_steady": round(steady_values_s, 2),
                    "confirmed_batches_per_s_steady": round(steady_values_s / POINTS_PER_BATCH, 2),
                    "backlog_slope_batches_per_s_steady": round(backlog_slope, 3),
                    "final_pending": final_pending,
                    "dlq_count": dlq,
                    "grpc_failures": grpc_failures,
                    "metrics_failures": metrics_failures,
                    "grpc_probes": len(samples),
                    "grpc_max_latency_ms": round(grpc_max_ms, 1),
                    "grpc_probe_p95_ms": round(grpc_p95_ms, 3),
                    "grpc_probe_p99_ms": round(grpc_p99_ms, 3),
                    "event_loop_lag_p99_max_ms": lag_p99_max,
                    "write_latency_p99_max_ms": write_p99_max,
                    "reliable_stage_diagnostics": stage_diagnostics,
                    "kafka_stage_diagnostics": kafka_diagnostics,
                    "quota_stage_diagnostics": quota_diagnostics,
                    "confirmed_subbatches_per_write": confirmed_subbatches_per_write,
                    "kafka_capacity_profile": {
                        "max_safe_inflight": KAFKA_MAX_SAFE_INFLIGHT,
                        "max_batch_size": KAFKA_MAX_BATCH_SIZE,
                        "linger_ms": KAFKA_LINGER_MS,
                        "partitions": KAFKA_PARTITIONS,
                    },
                    "cpu_percent_of_one_core": round(100 * cpu_seconds / (t_end - t0), 1),
                    "rss_max_mib": round(rss_max_mib, 1),
                    "redeliver_peak": redeliver_peak,
                    "drained": all(v == 0 for v in final_pending.values()),
                    "minimum_sustained_fraction": MIN_SUSTAINED_FRACTION,
                    "maximum_backlog_slope_fraction": MAX_BACKLOG_SLOPE_FRACTION,
                }

                # Offset 总数无法暴露“缺失 + 重复”的抵消；每个工况都按
                # farm/device/level/sample_time 身份做全量计数闭合。
                result["duplicate_audit"] = await _audit_duplicates(
                    sink_harness, confirmed_values, baseline_partitions
                )
                result["zero_loss"] = (
                    result["duplicate_audit"]["audit_distinct_identities"] == published_batches
                )
                result["identity_closed"] = (
                    result["duplicate_audit"]["audit_total_records"]
                    == confirmed_values // POINTS_PER_BATCH
                    and result["zero_loss"]
                    and not result["duplicate_audit"]["audit_stop_error"]
                )
                result["confirmed_payload_mb_total"] = round(
                    result["duplicate_audit"]["audit_payload_bytes"] / 1e6, 3
                )
                result["confirmed_payload_mb_per_s_end_to_end"] = round(
                    result["duplicate_audit"]["audit_payload_bytes"] / 1e6 / (t_end - t0), 3
                )

                samples_bytes = samples_path.read_bytes()
                result["samples_path"] = str(samples_path)
                result["samples_sha256"] = hashlib.sha256(samples_bytes).hexdigest()
                result["result_path"] = str(result_path)
                result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

                # ---- 硬不变量裁决 ----
                assert published_batches / wall_pub >= (
                    TARGET_BATCH_RATE * MIN_SUSTAINED_FRACTION
                ), result
                assert steady_puback_batches_s >= (
                    TARGET_BATCH_RATE * MIN_SUSTAINED_FRACTION
                ), result
                assert published_batches == pub_seq, result
                assert result["confirmed_batches_per_s_steady"] >= (
                    TARGET_BATCH_RATE * MIN_SUSTAINED_FRACTION
                ), result
                assert backlog_slope <= (TARGET_BATCH_RATE * MAX_BACKLOG_SLOPE_FRACTION), result
                assert grpc_failures == 0, result
                assert metrics_failures == 0, result
                assert dlq == 0, result
                assert order_audit["devices_observed"] == DEGREE, result
                assert order_audit["minimum_sequences_per_device"] >= 2, result
                assert not order_audit["order_or_partition_violations"], result
                assert not order_audit["order_stop_error"], result
                assert result["drained"], result
                assert confirmed_values >= published_values, result
                audit = result["duplicate_audit"]
                assert result["identity_closed"], result
                # 允许 at-least-once 重投，但不得有第三次投递或身份缺失。
                assert audit["audit_max_occurrence"] <= 2, result
            finally:
                if pub_task is not None and not pub_task.done():
                    pub_task.cancel()
                    await asyncio.gather(pub_task, return_exceptions=True)
                if encoder_pool is not None:
                    await asyncio.to_thread(encoder_pool.shutdown, wait=True, cancel_futures=True)
                await consumer.stop()
                await publisher.drain()
        finally:
            await sink_harness.stop()

    async def _enlarge_history_stream(self, harness: SinkSystemHarness) -> None:
        from nats.js.api import StreamConfig

        client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            info = await js.stream_info(harness.history_stream)
            current = info.config
            await js.update_stream(
                StreamConfig(
                    name=current.name,
                    subjects=list(current.subjects),
                    storage=current.storage,
                    retention=current.retention,
                    discard=current.discard,
                    max_bytes=STREAM_MAX_BYTES,
                    max_age=STREAM_MAX_AGE_S,
                )
            )
        finally:
            await client.drain()

    async def _wait_running(self, harness: SinkSystemHarness, delivery_id: str) -> None:
        from contracts import sink_service_pb2, sink_service_pb2_grpc

        deadline = asyncio.get_running_loop().time() + 45
        last: object = None
        while asyncio.get_running_loop().time() < deadline:
            async with harness.channel() as channel:
                response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
                    sink_service_pb2.ListDeliveriesRequest()
                )
            view = {item.delivery_id: item.actual_state for item in response.deliveries}
            last = view
            if view.get(delivery_id) == "RUNNING":
                return
            await asyncio.sleep(0.5)
        raise AssertionError(f"{delivery_id} never RUNNING: {last}")
