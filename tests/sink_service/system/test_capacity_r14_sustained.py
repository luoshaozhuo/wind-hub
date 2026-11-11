"""R14（v1_48）单实例 600s 持续容量验收（S4 长稳口径）。

与 R13 同环境、同权威口径对齐：
- 不使用 cProfile、不加任何生产插桩；证据全部来自进程边界之外——
  Kafka 端 offset（Broker 确认真值）、JetStream ConsumerInfo（服务端
  积压/重投真值）、/proc（CPU/RSS）、gRPC GetMetrics（事件环 P99、
  写延迟 P99）与 gRPC 可用性探针；
- 固定 500 点/批，开环令牌桶按全局目标批速率持续发布 R14_DURATION_S
  秒（默认 600s），发布结束后排空到零积压；
- 裁决以 Kafka 真实目标确认吞吐与「零丢失、DLQ=0、积压斜率≈0」为准，
  不以 Producer 发布速率代替；
- 关键参数走环境变量，便于先短烟测再正式长稳：
  R14_DURATION_S（默认 600）、R14_DEGREE（默认 2）、
  R14_TARGET_BATCH_RATE（全局批/s，默认 120）、R14_POINTS（默认 500）。

结果以 JSON 写入 /tmp/r14_capacity_<degree>_<rate>.json，供验收报告制表。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import nats
import pytest
from aiokafka import AIOKafkaConsumer

from contracts import common_pb2, common_pb2_grpc
from tests.sink_service.system.conftest import (
    SinkSystemHarness,
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
    pytest.mark.nats,
    pytest.mark.jetstream,
    pytest.mark.kafka,
]

DURATION_S = float(os.environ.get("R14_DURATION_S", "600"))
DEGREE = int(os.environ.get("R14_DEGREE", "2"))
TARGET_BATCH_RATE = float(os.environ.get("R14_TARGET_BATCH_RATE", "120"))
POINTS_PER_BATCH = int(os.environ.get("R14_POINTS", "500"))
DRAIN_TIMEOUT_S = 120.0
SAMPLE_INTERVAL_S = 1.0
# 系统夹具默认 history stream 仅 256MiB、max_age=7200s（供有界 S4 工况）。
# JetStream Limits 保留策略下 ACK 只推进消费者位置、不删除流消息，600s 持续
# 压测累积约 7 万批会按 max_bytes 撑爆（与消费快慢无关）。长稳专用：
# - 放大 max_bytes 到 R14_STREAM_MAX_BYTES（默认 4GiB）；
# - 加滚动 max_age=R14_STREAM_MAX_AGE_S（默认 180s），让已消费的老消息到期
#   回收，使流占用与运行时长解耦。消费者跟得上时（积压斜率≈0）到期消息早已
#   安全交付；name/subject/存储/retention/discard 均不变，仍满足生产启动守卫
#   （reliable_runtime 仅要求 limits + 有限 max_bytes/max_age，不限定值）。
STREAM_MAX_BYTES = int(os.environ.get("R14_STREAM_MAX_BYTES", str(4 * 1024**3)))
STREAM_MAX_AGE_S = int(os.environ.get("R14_STREAM_MAX_AGE_S", "180"))


def _devices(degree: int) -> list[str]:
    return [f"WT{i:03d}" for i in range(1, degree + 1)]


async def _grpc_probe(harness: SinkSystemHarness) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        async with harness.channel() as channel:
            await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
                common_pb2.Empty(), timeout=3
            )
        return True, (time.perf_counter() - start) * 1000
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


async def _audit_duplicates(harness: SinkSystemHarness, total_values: int) -> dict[str, Any]:
    """全量读 topic，按 (device, sample_id) 审计重复身份（S4 同口径）。"""
    audit = AIOKafkaConsumer(
        harness.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"qa-r14-audit-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    await audit.start()
    try:
        records: list[Any] = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and len(records) < total_values + 10000:
            batch = await audit.getmany(timeout_ms=2000, max_records=1000)
            for messages in batch.values():
                records.extend(messages)
            if not batch:
                break
        identities: dict[tuple[str, str], int] = {}
        for record in records:
            payload = json.loads(record.value.decode("utf-8"))
            key = (payload["device_id"], payload["sample_id"])
            identities[key] = identities.get(key, 0) + 1
        dupes = {key: count for key, count in identities.items() if count > 1}
        return {
            "audit_total_records": len(records),
            "audit_distinct_identities": len(identities),
            "audit_duplicate_identities": len(dupes),
            "audit_duplicate_record_excess": sum(c - 1 for c in dupes.values()),
            "audit_max_occurrence": max(dupes.values(), default=1),
        }
    finally:
        await audit.stop()


class TestR14SustainedCapacity:
    async def test_sustained_capacity_600s(
        self,
        sink_harness: SinkSystemHarness,
    ) -> None:
        devices = _devices(DEGREE)
        delivery_ids = [_delivery_id(i) for i in range(DEGREE)]
        _write_concurrent_root(sink_harness, devices=devices, delivery_ids=delivery_ids)
        await sink_harness.provision_streams()
        await self._enlarge_history_stream(sink_harness)
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, did) for did in delivery_ids}
        result_path = Path(f"/tmp/r14_capacity_d{DEGREE}_r{int(TARGET_BATCH_RATE)}.json")
        try:
            for did in delivery_ids:
                await self._wait_running(sink_harness, did)

            publisher = await nats.connect(servers=sink_harness.stack.nats, connect_timeout=10)
            consumer = AIOKafkaConsumer(
                bootstrap_servers=sink_harness.stack.kafka,
                client_id=f"qa-r14-cap-{sink_harness.token}",
            )
            await consumer.start()
            try:
                baseline = await _kafka_total_offset(consumer, sink_harness.kafka_topic)
                js = publisher.jetstream()
                interval = 1.0 / TARGET_BATCH_RATE if TARGET_BATCH_RATE > 0 else 0.0
                stop_at = time.monotonic() + DURATION_S
                published_batches = 0
                pub_seq = 0

                async def _publish_loop() -> None:
                    nonlocal published_batches, pub_seq
                    next_send = time.monotonic()
                    while time.monotonic() < stop_at:
                        device = devices[pub_seq % DEGREE]
                        batch = sink_harness.make_batch(
                            "reliable",
                            seq=pub_seq,
                            points=POINTS_PER_BATCH,
                            device_id=device,
                        )
                        await js.publish(batch.subject(), batch.encode(), timeout=10)
                        published_batches += 1
                        pub_seq += 1
                        next_send += interval
                        drift = next_send - time.monotonic()
                        if drift > 0:
                            await asyncio.sleep(drift)
                        else:
                            # 落后于调度时不追赶历史，避免突发，对齐到当前时刻。
                            next_send = time.monotonic()

                samples: list[dict[str, Any]] = []
                main_pid = sink_harness.process.proc.pid  # type: ignore[union-attr]
                t0 = time.monotonic()
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
                    samples.append(
                        {
                            "t": time.monotonic(),
                            "kafka_offset": await _kafka_total_offset(
                                consumer, sink_harness.kafka_topic
                            ),
                            "per_durable_pending": per_pending,
                            "redelivered": redelivered,
                            "cpu_ticks": ticks,
                            "rss_kb": rss_kb,
                            "grpc_ok": ok,
                            "grpc_ms": grpc_ms,
                            "lag_p99_ms": metrics.get("reliable_event_loop_lag_ms_p99"),
                            "write_latency_p99_ms": metrics.get(
                                "write_latency_ms_max_delivery_p99"
                            ),
                        }
                    )
                    await asyncio.sleep(SAMPLE_INTERVAL_S)
                await pub_task
                t_pub_end = time.monotonic()

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
                t_end = time.monotonic()

                published_values = published_batches * POINTS_PER_BATCH
                confirmed_values = final_offset - baseline
                wall_pub = max(t_pub_end - t0, 1e-6)

                # 稳态窗口：持续期 10%→90%，排除预热与排空。
                offsets = [
                    (s["t"], s["kafka_offset"] - baseline)
                    for s in samples
                    if t0 + 0.1 * DURATION_S <= s["t"] <= t0 + 0.9 * DURATION_S
                ]
                if len(offsets) >= 2:
                    dt = offsets[-1][0] - offsets[0][0]
                    steady_values_s = (offsets[-1][1] - offsets[0][1]) / dt if dt > 0 else 0.0
                else:
                    steady_values_s = 0.0

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
                cpu_seconds = (last["cpu_ticks"] - first["cpu_ticks"]) / CLK_TCK
                grpc_failures = sum(1 for s in samples if not s["grpc_ok"])
                grpc_max_ms = max(s["grpc_ms"] for s in samples)
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

                result = {
                    "scenario": "r14_sustained_reliable",
                    "history_stream_max_bytes": STREAM_MAX_BYTES,
                    "history_stream_max_age_s": STREAM_MAX_AGE_S,
                    "duration_s": DURATION_S,
                    "degree": DEGREE,
                    "target_batch_rate_global": TARGET_BATCH_RATE,
                    "points_per_batch": POINTS_PER_BATCH,
                    "published_batches": published_batches,
                    "published_values": published_values,
                    "confirmed_values": confirmed_values,
                    "zero_loss": confirmed_values == published_values,
                    "publish_wall_seconds": round(wall_pub, 3),
                    "achieved_publish_batches_per_s": round(published_batches / wall_pub, 2),
                    "confirmed_values_per_s_steady": round(steady_values_s, 2),
                    "confirmed_batches_per_s_steady": round(steady_values_s / POINTS_PER_BATCH, 2),
                    "backlog_slope_batches_per_s_steady": round(backlog_slope, 3),
                    "final_pending": final_pending,
                    "dlq_count": dlq,
                    "grpc_failures": grpc_failures,
                    "grpc_probes": len(samples),
                    "grpc_max_latency_ms": round(grpc_max_ms, 1),
                    "event_loop_lag_p99_max_ms": lag_p99_max,
                    "write_latency_p99_max_ms": write_p99_max,
                    "cpu_percent_of_one_core": round(100 * cpu_seconds / (t_end - t0), 1),
                    "rss_max_mib": round(rss_max_mib, 1),
                    "redeliver_peak": redeliver_peak,
                    "drained": all(v == 0 for v in final_pending.values()),
                }

                # 仅当出现超额（重投重复）时做全量审计，零丢失常态下省一轮读盘。
                if confirmed_values > published_values:
                    result["duplicate_audit"] = await _audit_duplicates(
                        sink_harness, published_values
                    )

                result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

                # ---- 硬不变量裁决 ----
                assert grpc_failures == 0, result
                assert dlq == 0, result
                assert result["drained"], result
                assert confirmed_values >= published_values, result
                if confirmed_values > published_values:
                    audit = result["duplicate_audit"]
                    # 允许 at-least-once 重投，但不得有第三次投递（无未知重复）。
                    assert audit["audit_max_occurrence"] == 2, result
            finally:
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
