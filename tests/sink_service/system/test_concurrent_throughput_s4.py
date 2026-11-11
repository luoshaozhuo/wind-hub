"""v1_23 设备级并行 Reliable 交付的真实链路吞吐验收（S4）。

硬约束（任务书）：

- 不使用 cProfile，不增加任何生产插桩；全部证据来自进程边界之外——
  Kafka 端 offset（Broker 确认真值）、JetStream ConsumerInfo（服务端
  积压/重投真值）、/proc（CPU/RSS）、gRPC GetMetrics（事件环 P99、
  写延迟 P99）与 gRPC 可用性探针；
- 固定 500 点/批，并行度 1/2/4/8，每度 3 轮，分 Fresh / Backlog 两态；
- 性能裁决以 Kafka 真实目标确认吞吐为准，不以 Producer 发布速率代替；
- Backlog 消息全部老化 >5s，逐 Delivery 受 replay_max_rate_per_second
  =50 批/s 限速，必须在报告中显式给出该上限与聚合负载的关系。

结果以 JSON 追加写入 /tmp/r7_s4_results.json，供验收报告制表。
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
from aiokafka.structs import TopicPartition

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

POINTS_PER_BATCH = 500
DEGREES = [1, 2, 4, 8]
ROUNDS = 3
BATCHES_PER_DEVICE = 150
SAMPLE_INTERVAL_S = 0.25
CASE_TIMEOUT_S = 300.0
RESULTS_PATH = Path("/tmp/r7_s4_results.json")
CLK_TCK = os.sysconf("SC_CLK_TCK")


def _devices(degree: int) -> list[str]:
    return [f"WT{i:03d}" for i in range(1, degree + 1)]


def _proc_cpu_rss(pids: set[int]) -> tuple[int, int]:
    """返回 (utime+stime clock ticks, VmRSS KiB)；进程退出按 0 计。"""
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


async def _kafka_total_offset(consumer: AIOKafkaConsumer, topic: str) -> int:
    # aiokafka 0.14：partitions_for_topic 是同步方法且首次元数据未就绪时
    # 返回 None，需要经 force_metadata_update 拉取后重试。
    partitions = consumer.partitions_for_topic(topic)
    attempts = 0
    while partitions is None and attempts < 5:
        await consumer._client.force_metadata_update()  # noqa: SLF001
        partitions = consumer.partitions_for_topic(topic)
        attempts += 1
    if not partitions:
        return 0
    tps = [TopicPartition(topic, partition) for partition in partitions]
    ends = await consumer.end_offsets(tps)
    return sum(ends.values())


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


async def _sample_loop(
    harness: SinkSystemHarness,
    *,
    consumer: AIOKafkaConsumer,
    names: set[str],
    deadline: float,
    should_stop: Any,
) -> list[dict[str, Any]]:
    """1s 间隔同步采样，直到 drained() 为真或超时。"""
    samples: list[dict[str, Any]] = []
    main_pid = harness.process.proc.pid  # type: ignore[union-attr]
    while time.monotonic() < deadline:
        pids = worker_pid_set(main_pid) | {main_pid}
        ticks, rss_kb = _proc_cpu_rss(pids)
        infos = await _consumer_infos(harness)
        per_durable = {
            name: (infos[name].num_pending + infos[name].num_ack_pending if name in infos else None)
            for name in names
        }
        redelivered = {
            name: infos[name].num_redelivered if name in infos else None for name in names
        }
        ok, grpc_ms = await _grpc_probe(harness)
        metrics = await _fetch_metrics(harness)
        samples.append(
            {
                "t": time.monotonic(),
                "kafka_offset": await _kafka_total_offset(consumer, harness.kafka_topic),
                "per_durable_pending": per_durable,
                "redelivered": redelivered,
                "cpu_ticks": ticks,
                "rss_kb": rss_kb,
                "grpc_ok": ok,
                "grpc_ms": grpc_ms,
                "worker_pids": sorted(worker_pid_set(main_pid)),
                "lag_p99_ms": metrics.get("reliable_event_loop_lag_ms_p99"),
                "write_latency_p99_ms": metrics.get("write_latency_ms_max_delivery_p99"),
            }
        )
        if should_stop(per_durable):
            break
        await asyncio.sleep(SAMPLE_INTERVAL_S)
    # 末帧强制刷新元数据后再读一次高水位，避免最后一次采样落在元数据
    # 传播窗口内把 Broker 已确认记录误读为 0。
    await consumer._client.force_metadata_update()  # noqa: SLF001
    final_ticks, final_rss = _proc_cpu_rss(worker_pid_set(main_pid) | {main_pid})
    final_infos = await _consumer_infos(harness)
    ok, grpc_ms = await _grpc_probe(harness)
    metrics = await _fetch_metrics(harness)
    samples.append(
        {
            "t": time.monotonic(),
            "kafka_offset": await _kafka_total_offset(consumer, harness.kafka_topic),
            "per_durable_pending": {
                name: (
                    final_infos[name].num_pending + final_infos[name].num_ack_pending
                    if name in final_infos
                    else None
                )
                for name in names
            },
            "redelivered": {
                name: final_infos[name].num_redelivered if name in final_infos else None
                for name in names
            },
            "cpu_ticks": final_ticks,
            "rss_kb": final_rss,
            "grpc_ok": ok,
            "grpc_ms": grpc_ms,
            "worker_pids": sorted(worker_pid_set(main_pid)),
            "lag_p99_ms": metrics.get("reliable_event_loop_lag_ms_p99"),
            "write_latency_p99_ms": metrics.get("write_latency_ms_max_delivery_p99"),
        }
    )
    return samples


def _summarize(
    samples: list[dict[str, Any]],
    *,
    t_start: float,
    t_end: float,
    degree: int,
    mode: str,
    round_index: int,
    total_batches: int,
    total_values: int,
    baseline_offset: int,
) -> dict[str, Any]:
    offsets = [(sample["t"], sample["kafka_offset"] - baseline_offset) for sample in samples]
    # 稳态斜率：确认量 10%→90% 区间的 Broker 确认吞吐（权威口径）。
    final_confirmed = offsets[-1][1]
    low, high = final_confirmed * 0.1, final_confirmed * 0.9
    picked = [(t, off) for t, off in offsets if low <= off <= high]
    if len(picked) >= 2:
        dt = picked[-1][0] - picked[0][0]
        steady_values_s = (picked[-1][1] - picked[0][1]) / dt if dt > 0 else 0.0
    else:
        steady_values_s = 0.0
    # overall 速率从首个 Broker 确认点起算，排除进程启动/预热时间。
    confirmed_series = [(t, off) for t, off in offsets if off > 0]
    t_first_confirm = confirmed_series[0][0] if confirmed_series else t_start
    wall = max(t_end - t_first_confirm, 1e-6)
    first = samples[0]
    last = samples[-1]
    cpu_seconds = (last["cpu_ticks"] - first["cpu_ticks"]) / CLK_TCK
    grpc_failures = sum(1 for sample in samples if not sample["grpc_ok"])
    grpc_max_ms = max(sample["grpc_ms"] for sample in samples)
    lag_p99_max = max(
        (sample["lag_p99_ms"] for sample in samples if sample["lag_p99_ms"] is not None),
        default=None,
    )
    write_p99_max = max(
        (
            sample["write_latency_p99_ms"]
            for sample in samples
            if sample["write_latency_p99_ms"] is not None
        ),
        default=None,
    )
    rss_max_kb = max(sample["rss_kb"] for sample in samples)
    redelivered = {
        name: last["redelivered"][name] for name in last["redelivered"] if last["redelivered"][name]
    }
    # worker 重生检测：采样期 PID 集合变化即说明 supervisor 重建过 worker
    # （新 producer epoch），是 Kafka 端重复来源的关键诊断。
    pid_generations = {tuple(sample["worker_pids"]) for sample in samples}
    worker_respawned = len(pid_generations) > 1
    # 每设备积压斜率：取该 durable 首次与末次非零 pending 的时间差。
    backlog_slopes: dict[str, float] = {}
    for name in names_from(samples):
        series = [
            (sample["t"], sample["per_durable_pending"][name])
            for sample in samples
            if sample["per_durable_pending"].get(name) is not None
        ]
        nonzero = [(t, pending) for t, pending in series if pending > 0]
        if len(nonzero) >= 2:
            dt = nonzero[-1][0] - nonzero[0][0]
            backlog_slopes[name] = (nonzero[0][1] - nonzero[-1][1]) / dt if dt > 0 else 0.0
    return {
        "mode": mode,
        "degree": degree,
        "round": round_index,
        "total_batches": total_batches,
        "total_values": total_values,
        "confirmed_values": final_confirmed,
        "wall_seconds": round(wall, 3),
        "confirmed_batches_per_s_overall": round(total_batches / wall, 2),
        "confirmed_values_per_s_overall": round(final_confirmed / wall, 2),
        "confirmed_values_per_s_steady": round(steady_values_s, 2),
        "cpu_seconds": round(cpu_seconds, 3),
        "cpu_percent_of_one_core": round(100 * cpu_seconds / wall, 1),
        "rss_max_mib": round(rss_max_kb / 1024, 1),
        "event_loop_lag_p99_max_ms": lag_p99_max,
        "write_latency_p99_max_ms": write_p99_max,
        "grpc_failures": grpc_failures,
        "grpc_probes": len(samples),
        "grpc_max_latency_ms": round(grpc_max_ms, 1),
        "redelivered_nonzero": redelivered,
        # JetStream num_redelivered 是瞬时在途计数：重投消息被 ACK 后即清零，
        # 且重投窗口可短至 ~20ms，1s 采样帧大概率错过。保留跨帧峰值仅作佐证，
        # 重投事实的权威对账改由 Kafka 审计指纹承担（见 _assert_invariants）。
        "redelivered_peak": {
            name: peak
            for name, peak in (
                (
                    sample_name,
                    max(frame["redelivered"].get(sample_name) or 0 for frame in samples),
                )
                for sample_name in names_from(samples)
            )
            if peak
        },
        "worker_respawned": worker_respawned,
        "worker_pid_generations": [list(gen) for gen in sorted(pid_generations)],
        "backlog_drain_batches_per_s_per_durable": {
            name: round(rate, 2) for name, rate in backlog_slopes.items()
        },
    }


def names_from(samples: list[dict[str, Any]]) -> list[str]:
    return sorted(samples[0]["per_durable_pending"])


def _append_result(result: dict[str, Any]) -> None:
    existing = json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else []
    existing.append(result)
    RESULTS_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


class TestReliableParallelThroughput:
    @pytest.mark.parametrize("degree", DEGREES)
    @pytest.mark.parametrize("round_index", range(1, ROUNDS + 1))
    async def test_s4_fresh_throughput(
        self,
        sink_harness: SinkSystemHarness,
        degree: int,
        round_index: int,
    ) -> None:
        devices = _devices(degree)
        delivery_ids = [_delivery_id(i) for i in range(degree)]
        _write_concurrent_root(sink_harness, devices=devices, delivery_ids=delivery_ids)
        await sink_harness.provision_streams()
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, did) for did in delivery_ids}
        total_batches = BATCHES_PER_DEVICE * degree
        total_values = total_batches * POINTS_PER_BATCH
        try:
            for did in delivery_ids:
                await _wait_running(sink_harness, did)
            publisher = await nats.connect(servers=sink_harness.stack.nats, connect_timeout=10)
            consumer = AIOKafkaConsumer(
                bootstrap_servers=sink_harness.stack.kafka,
                client_id=f"qa-s4-fresh-{sink_harness.token}",
            )
            await consumer.start()
            try:
                baseline = await _kafka_total_offset(consumer, sink_harness.kafka_topic)
                js = publisher.jetstream()

                async def _publish_all() -> None:
                    for seq in range(BATCHES_PER_DEVICE):
                        for device in devices:
                            batch = sink_harness.make_batch(
                                "reliable",
                                seq=seq,
                                points=POINTS_PER_BATCH,
                                device_id=device,
                            )
                            await js.publish(batch.subject(), batch.encode(), timeout=10)

                t_start = time.monotonic()
                publish_task = asyncio.create_task(_publish_all())

                def _fresh_stop(per: dict[str, Any]) -> bool:
                    # 发布完成前禁止因瞬时零积压提前退出。
                    return publish_task.done() and all(value == 0 for value in per.values())

                samples = await _sample_loop(
                    sink_harness,
                    consumer=consumer,
                    names=names,
                    deadline=t_start + CASE_TIMEOUT_S,
                    should_stop=_fresh_stop,
                )
                await publish_task
                t_end = samples[-1]["t"]
                result = _summarize(
                    samples,
                    t_start=t_start,
                    t_end=t_end,
                    degree=degree,
                    mode="fresh",
                    round_index=round_index,
                    total_batches=total_batches,
                    total_values=total_values,
                    baseline_offset=baseline,
                )
                result["replay_rate_limit_note"] = (
                    "fresh: num_delivered=1 且 age<=5s，不触发 50 批/s 限速"
                )
                await self._assert_invariants(
                    sink_harness, names, result, total_values, set(_devices(degree))
                )
                _append_result(result)
            finally:
                await consumer.stop()
                await publisher.drain()
        finally:
            await sink_harness.stop()

    @pytest.mark.parametrize("degree", DEGREES)
    @pytest.mark.parametrize("round_index", range(1, ROUNDS + 1))
    async def test_s4_backlog_throughput(
        self,
        sink_harness: SinkSystemHarness,
        degree: int,
        round_index: int,
    ) -> None:
        devices = _devices(degree)
        delivery_ids = [_delivery_id(i) for i in range(degree)]
        _write_concurrent_root(sink_harness, devices=devices, delivery_ids=delivery_ids)
        await sink_harness.provision_streams()
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, did) for did in delivery_ids}
        total_batches = BATCHES_PER_DEVICE * degree
        total_values = total_batches * POINTS_PER_BATCH
        # 进程启动前预压全部积压，再静置 >5s 保证全部按 historical 限速消费。
        publisher = await nats.connect(servers=sink_harness.stack.nats, connect_timeout=10)
        try:
            js = publisher.jetstream()
            for seq in range(BATCHES_PER_DEVICE):
                for device in devices:
                    batch = sink_harness.make_batch(
                        "reliable", seq=seq, points=POINTS_PER_BATCH, device_id=device
                    )
                    await js.publish(batch.subject(), batch.encode(), timeout=10)
            # 发布侧真值对账：Stream 消息数必须恰为发布批数，排除 NATS
            # 重连重发造成的源侧重复（与 Sink 处理侧重复区分）。
            stream_info = await js.stream_info(sink_harness.history_stream)
            source_messages = int(stream_info.state.messages)
        finally:
            await publisher.drain()
        assert (
            source_messages == total_batches
        ), f"source stream duplicate/loss: {source_messages} != {total_batches}"
        await asyncio.sleep(6)
        consumer = AIOKafkaConsumer(
            bootstrap_servers=sink_harness.stack.kafka,
            client_id=f"qa-s4-backlog-{sink_harness.token}",
        )
        await consumer.start()
        try:
            baseline = await _kafka_total_offset(consumer, sink_harness.kafka_topic)
            t_start = time.monotonic()
            await sink_harness.start()
            try:
                for did in delivery_ids:
                    await _wait_running(sink_harness, did)
                samples = await _sample_loop(
                    sink_harness,
                    consumer=consumer,
                    names=names,
                    deadline=t_start + CASE_TIMEOUT_S,
                    should_stop=lambda per: all(value == 0 for value in per.values()),
                )
                t_end = samples[-1]["t"]
                result = _summarize(
                    samples,
                    t_start=t_start,
                    t_end=t_end,
                    degree=degree,
                    mode="backlog",
                    round_index=round_index,
                    total_batches=total_batches,
                    total_values=total_values,
                    baseline_offset=baseline,
                )
                result["replay_rate_limit_note"] = (
                    "backlog: 全部 age>5s 判为 historical，逐 Delivery 限速 50 批/s；"
                    f"{degree} Delivery 聚合需求 {50 * degree} 批/s，"
                    "实际另受 Sink 额度 min(per_sink=8, max_safe_inflight=4)=4 约束"
                )
                await self._assert_invariants(
                    sink_harness, names, result, total_values, set(_devices(degree))
                )
                _append_result(result)
            finally:
                await sink_harness.stop()
        finally:
            await consumer.stop()

    async def _assert_invariants(
        self,
        harness: SinkSystemHarness,
        names: set[str],
        result: dict[str, Any],
        total_values: int,
        expected_devices: set[str],
    ) -> None:
        infos = await _consumer_infos(harness)
        redelivery = {name: info.num_redelivered for name, info in infos.items() if name in names}
        assert result["grpc_failures"] == 0, result
        assert await harness.dlq_count() == 0
        assert all(
            info.num_pending + info.num_ack_pending == 0
            for name, info in infos.items()
            if name in names
        ), redelivery
        # allow_at_least_once 策略下重投重复的权威对账基于 Kafka 审计指纹，
        # 而非 JetStream num_redelivered：后者是瞬时在途计数，重投消息 ACK 后
        # 立即清零，且重投窗口可短至 ~20ms，终态/1s 采样均无法稳定捕获。
        # 合法重投指纹：同一 (设备, sample_id) 身份恰好出现 2 次，重复设备属于
        # 本工况参与集合，且每设备每批的重复批内下标构成从 0 起的连续前缀
        # （首次投递在 broker 已确认前缀后停滞、整批重投所致；一次缓存清空可
        # 同时打中多个协程，故不要求单设备）；任何第三次投递、集合外设备或
        # 下标空洞均视为未知重复。
        extra = result["confirmed_values"] - total_values
        if extra:
            # 诊断审计：全量读取 topic，按 (device, sample_id) 统计重复身份。
            audit = AIOKafkaConsumer(
                harness.kafka_topic,
                bootstrap_servers=harness.stack.kafka,
                auto_offset_reset="earliest",
                group_id=f"qa-s4-audit-{uuid.uuid4().hex[:10]}",
                enable_auto_commit=False,
            )
            await audit.start()
            try:
                audit_records = []
                audit_deadline = time.monotonic() + 60
                while (
                    time.monotonic() < audit_deadline and len(audit_records) < total_values + 10000
                ):
                    batch = await audit.getmany(timeout_ms=2000, max_records=1000)
                    for messages in batch.values():
                        audit_records.extend(messages)
                    if not batch:
                        break
                identities: dict[tuple[str, str], int] = {}
                occurrences: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
                for record in audit_records:
                    payload = json.loads(record.value.decode("utf-8"))
                    key = (payload["device_id"], payload["sample_id"])
                    identities[key] = identities.get(key, 0) + 1
                    occurrences.setdefault(key, []).append(
                        (record.partition, record.offset, record.timestamp)
                    )
                dupes = {key: count for key, count in identities.items() if count > 1}
                result["audit_total_records"] = len(audit_records)
                result["audit_duplicate_identities"] = [
                    {
                        "device": key[0],
                        "sample_id": key[1],
                        "count": count,
                        "locations": occurrences[key],
                    }
                    for key, count in sorted(dupes.items(), key=lambda item: -item[1])[:10]
                ]
                duplicate_excess = sum(count - 1 for count in identities.values() if count > 1)
                result["audit_duplicate_record_excess"] = duplicate_excess
                duplicate_devices = {key[0] for key in dupes}
                result["audit_duplicate_devices"] = sorted(duplicate_devices)
                result["audit_duplicate_per_device"] = {
                    device: sum(1 for key in dupes if key[0] == device)
                    for device in sorted(duplicate_devices)
                }
                # 任何超额轮先落完整证据（含全部富集字段），避免 pytest 字典
                # repr 截断丢失现场。
                Path("/tmp/r7_s4_anomaly.json").write_text(json.dumps(result, indent=2))
                # 指纹 1：每个重复身份恰好 2 次（一次重投，无第三次投递）。
                triple = [key for key, count in dupes.items() if count != 2]
                assert not triple, {"non_pair_duplicates": triple[:5], "result": result}
                # 指纹 2：重复设备必须属于本工况参与设备集合。metadata listener
                # 一次 clear 清空整个共享缓存，多个停在 await send 的协程可各自
                # 恢复后独立撞 KeyError，因此同一轮允许多设备同时受害（单设备
                # 只是早期小样本观察，非生产不变量）；但不得出现参与集合外设备。
                unknown_devices = duplicate_devices - expected_devices
                assert not unknown_devices, {
                    "unknown_devices": unknown_devices,
                    "duplicate_devices": duplicate_devices,
                    "result": result,
                }
                # 指纹 3：sample_id 形如 s-reliable-{seq}-{idx}-{token}，
                # 每设备每批的重复下标必须是 0..k-1 连续前缀（按设备独立校验，
                # 多设备各自重投互不混淆）。
                per_seq_indexes: dict[tuple[str, int], set[int]] = {}
                for device, sample_id in dupes:
                    parts = sample_id.split("-")
                    per_seq_indexes.setdefault((device, int(parts[-3])), set()).add(int(parts[-2]))
                prefix_violations = {
                    f"{device}:seq{seq}": sorted(indexes)
                    for (device, seq), indexes in per_seq_indexes.items()
                    if indexes != set(range(len(indexes)))
                }
                assert not prefix_violations, {
                    "prefix_violations": prefix_violations,
                    "result": result,
                }
                # 指纹 4：Kafka 超额记录数与重复身份超额完全相等，无丢失/幽灵记录。
                assert extra == duplicate_excess, {
                    "extra_values": extra,
                    "duplicate_excess": duplicate_excess,
                    "result": result,
                }
            finally:
                await audit.stop()
        else:
            assert sum(redelivery.values()) == 0, redelivery
        # num_redelivered 瞬时值仅记录：通过不代表无重投（ACK 后清零），
        # 非零也只佐证审计结论，不再作为重投批次的硬对账来源。
        result["redelivered_batches_observed"] = sum(redelivery.values())
        result["redelivered_peak_observed"] = result.get("redelivered_peak", {})


async def _wait_running(harness: SinkSystemHarness, delivery_id: str) -> None:
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
