"""GatewayObservability 纯逻辑：九指标集、nearest-rank P99、滑动窗口、事件上限。

仅通过公共 API（record/latency/pending/event + start 后 metrics/query_events）
驱动，不触达私有方法。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from industrial_gateway.infrastructure.observability import GatewayObservability

NINE_METRICS = {
    "update_batches_total",
    "updated_variables_total",
    "client_writes_total",
    "forward_accepted_total",
    "forward_unknown_total",
    "updated_variables_rate_per_second",
    "update_latency_ms_p99",
    "control_events_pending",
    "metrics_covered_ms",
}


async def _started(window_seconds: int = 60) -> GatewayObservability:
    obs = GatewayObservability(window_seconds=window_seconds, interval_ms=100)
    obs.start(lambda: ("gw1", "gw2"))
    return obs


async def _snapshot(obs: GatewayObservability, scope: str = "") -> dict[str, float]:
    for _ in range(30):
        reply = obs.metrics(scope, obs.window_seconds * 1000)
        if reply.metrics:
            return {metric.name: metric.value for metric in reply.metrics}
        await asyncio.sleep(0.05)
    raise AssertionError("metrics snapshot never became ready")


def test_snapshot_exposes_nine_named_metrics_with_units() -> None:
    async def scenario() -> None:
        obs = await _started()
        try:
            obs.record("gw1", "update_batches_total")
            obs.latency("gw1", 1.0)
            values = await _snapshot(obs)
            assert set(values) == NINE_METRICS
            assert values["update_batches_total"] == 1
            assert values["metrics_covered_ms"] > 0
            reply = obs.metrics("", 60_000)
            assert all(metric.unit for metric in reply.metrics)
            # 进程刚启动时 covered < 窗口，window_ms 如实缩短而非谎称满窗。
            assert all(0 < metric.window_ms <= 60_000 for metric in reply.metrics)
        finally:
            await obs.stop()

    asyncio.run(scenario())


def test_p99_uses_nearest_rank_ceil_99n() -> None:
    async def scenario() -> None:
        obs = await _started()
        try:
            for ms in range(1, 101):  # 1..100ms
                obs.latency("gw1", float(ms))
            values = await _snapshot(obs)
            # nearest-rank: ceil(0.99*100)-1（0-based）→ 排序后第 99 个 = 99ms。
            assert values["update_latency_ms_p99"] == 99.0
        finally:
            await obs.stop()

    asyncio.run(scenario())


def test_scope_filters_counters() -> None:
    async def scenario() -> None:
        obs = await _started()
        try:
            obs.record("gw1", "update_batches_total", 3)
            obs.record("gw2", "update_batches_total", 5)
            gw1 = await _snapshot(obs, "gw1")
            assert gw1["update_batches_total"] == 3
            aggregate = await _snapshot(obs, "")
            assert aggregate["update_batches_total"] == 8
        finally:
            await obs.stop()

    asyncio.run(scenario())


def test_sliding_window_drives_rate_but_counters_remain_epoch() -> None:
    async def scenario() -> None:
        obs = await _started(window_seconds=1)
        try:
            obs.record("gw1", "updated_variables_total", 10)
            obs.pending("gw1", 2)
            await asyncio.sleep(1.1)  # 滑出 1s 窗口。
            values = await _snapshot(obs)
            # 计数器为进程纪元累计、不衰减；速率只统计窗口内样本 → 0。
            assert values["updated_variables_total"] == 10
            assert values["updated_variables_rate_per_second"] == 0
            # pending 是即时读数，不随窗口衰减。
            assert values["control_events_pending"] == 2
        finally:
            await obs.stop()

    asyncio.run(scenario())


def test_metrics_rejects_mismatched_window() -> None:
    obs = GatewayObservability(window_seconds=60, interval_ms=1000)
    with pytest.raises(ValueError):
        obs.metrics("", 30_000)


def test_record_rejects_unknown_metric() -> None:
    obs = GatewayObservability(window_seconds=60, interval_ms=1000)
    with pytest.raises(ValueError):
        obs.record("gw1", "not_a_metric")


def test_events_are_bounded_filtered_and_paginated() -> None:
    obs = GatewayObservability(window_seconds=60, interval_ms=1000)
    for index in range(5000):
        obs.event("gw1", f"message-{index}", error_code=f"E{index % 3}")
    assert len(obs.events) == 4096  # 进程内有界环形，不无限增长。

    reply = obs.query_events(filter_text="E0", page_size=10)
    assert len(reply.events) == 10
    assert all("E0" in event.error_code for event in reply.events)
    assert reply.next_page_token == "10"
    page2 = obs.query_events(filter_text="E0", page_size=10, page_token=reply.next_page_token)
    assert len(page2.events) == 10
    assert {event.event_id for event in reply.events}.isdisjoint(
        {event.event_id for event in page2.events}
    )

    with pytest.raises(ValueError):
        obs.query_events(page_token="abc")

    now = datetime.now(UTC)
    future = obs.query_events(from_time=now + timedelta(days=1))
    assert future.events == []
