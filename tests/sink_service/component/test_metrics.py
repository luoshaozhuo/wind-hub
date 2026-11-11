"""R04 observability contract: twelve metric names, window math and event ring.

These are deterministic process-local tests against the real ``Observability``
class (no mocks). They pin the design section 9.3 / R04 vocabulary and the
edge-case semantics a cold or degraded process must expose honestly:

- cold snapshot carries only ``metrics_snapshot_ready=0`` and invents nothing;
- unsupported windows are rejected, never silently recomputed;
- ratio / P99 / rate are omitted rather than fabricated when their denominator
  or sample set is empty;
- the latency window is half-open (cutoff excluded, now included);
- the event ring is bounded, cursor-paged and rejects expired cursors.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from sink_service.application.latency_window import LatencyWindow
from sink_service.application.observability import Observability

pytestmark = [pytest.mark.component, pytest.mark.contract]

WINDOW_MS = 60000
# The exact process metric vocabulary: twelve R04 names (design R04 / 9.3.2)
# plus the eight batch-series counters emitted by the Realtime/Reliable workers.
R04_METRICS = {
    "received_total",
    "delivered_total",
    "failed_total",
    "dropped_total",
    "dead_letter_total",
    "delivered_rate_per_second",
    "write_success_ratio",
    "write_latency_ms_p99",
    "backlog_messages",
    "io_inflight",
    "metrics_snapshot_ready",
    "metrics_covered_ms",
    "input_batches_total",
    "input_values_total",
    "acknowledged_batches_total",
    "rejected_batches_total",
    "routed_values_total",
    "retry_total",
    "dead_letter_invalid_batches_total",
}


def test_cold_snapshot_is_honestly_not_ready() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    snap = obs.snapshot(WINDOW_MS)
    assert snap == {"metrics_snapshot_ready": 0.0}
    # No delivery scope has ever been sampled.
    assert obs.delivery_snapshot(WINDOW_MS) == {}


def test_unsupported_window_is_rejected() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    with pytest.raises(ValueError, match="unsupported metrics window"):
        obs.snapshot(1000)
    with pytest.raises(ValueError, match="unsupported metrics window"):
        obs.delivery_snapshot(1000)


def test_retired_received_batches_counter_is_never_emitted() -> None:
    """旧名 received_batches_total 已退役：即使 worker 误报也不得出现在快照。"""
    obs = Observability(window_ms=WINDOW_MS)
    obs.sample({"input_batches_total": 3, "received_batches_total": 3})
    snap = obs.snapshot(WINDOW_MS)
    assert snap["input_batches_total"] == 3.0
    assert "received_batches_total" not in snap
    assert "received_batches_total" not in R04_METRICS


def test_sampled_counters_stay_inside_r04_vocabulary() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    obs.sample(
        {
            "received_total": 10,
            "delivered_total": 8,
            "failed_total": 1,
            "dropped_total": 1,
            "dead_letter_total": 0,
            "io_inflight": 2,
            "backlog_messages": 4,
        }
    )
    snap = obs.snapshot(WINDOW_MS)
    assert snap["metrics_snapshot_ready"] == 1.0
    assert set(snap) <= R04_METRICS
    # Six monotonic counters plus the optional backlog pass through verbatim.
    assert snap["received_total"] == 10.0
    assert snap["delivered_total"] == 8.0
    assert snap["failed_total"] == 1.0
    assert snap["dropped_total"] == 1.0
    assert snap["dead_letter_total"] == 0.0
    assert snap["io_inflight"] == 2.0
    assert snap["backlog_messages"] == 4.0
    assert 0 < snap["metrics_covered_ms"] <= WINDOW_MS
    # delivered / covered-seconds is present once the window has any coverage.
    assert "delivered_rate_per_second" in snap


def test_backlog_omitted_when_not_reported() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    obs.sample({"received_total": 1})
    assert "backlog_messages" not in obs.snapshot(WINDOW_MS)


def test_write_success_ratio_requires_a_denominator() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    # No write_success_total and no failures: ratio must not be invented.
    obs.sample({"delivered_total": 0, "failed_total": 0})
    assert "write_success_ratio" not in obs.snapshot(WINDOW_MS)

    obs2 = Observability(window_ms=WINDOW_MS)
    obs2.sample({"write_success_total": 7, "failed_total": 3})
    assert obs2.snapshot(WINDOW_MS)["write_success_ratio"] == pytest.approx(0.7)


def test_p99_omitted_without_latency_samples_and_uses_nearest_rank() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    obs.sample({"delivered_total": 1})  # no latency histogram
    assert "write_latency_ms_p99" not in obs.snapshot(WINDOW_MS)

    # B2：最近秩 P99 在有界加权对数桶上计算（ceil(N*0.99)=99/100），
    # 报告命中桶上边界，相对误差小于 1%，而不是悄悄填零或截断样本。
    obs2 = Observability(window_ms=WINDOW_MS)
    now = datetime.now(UTC)
    window = LatencyWindow(window_ms=WINDOW_MS)
    for i in range(100):
        window.add(now, float(i))
    bins = LatencyWindow.window_bins((window.snapshot(),), now=now, window_ms=WINDOW_MS)
    expected = LatencyWindow.percentile_bins(Counter(bins))
    obs2.sample({"delivered_total": 100}, process_latency_bins=bins)
    reported = obs2.snapshot(WINDOW_MS)["write_latency_ms_p99"]
    assert reported == pytest.approx(expected)
    # 对数桶多值共桶时命中桶上界可能略低于真实第 99 秩值，设计保证相对误差 <1%
    assert abs(reported - 99.0) < 0.01 * (1.0 + 99.0)


def test_latency_window_is_half_open_and_overflow_suppresses_p99() -> None:
    now = datetime.now(UTC)
    window = LatencyWindow(window_ms=WINDOW_MS)
    window.add(now - timedelta(seconds=120), 999.0)  # older than the 60s cutoff
    window.add(now - timedelta(seconds=5), 10.0)
    inside_bins = LatencyWindow.window_bins((window.snapshot(),), now=now, window_ms=WINDOW_MS)
    # 半开窗口：cutoff 排除、now 包含，窗口外的 999ms 样本不参与 P99
    assert inside_bins.total() == 1
    obs = Observability(window_ms=WINDOW_MS)
    obs.sample({"delivered_total": 1}, process_latency_bins=inside_bins)
    reported = obs.snapshot(WINDOW_MS)["write_latency_ms_p99"]
    assert 10.0 <= reported < 11.0  # 仅窗口内 10ms 样本参与，桶上界误差 <1%

    # 溢出桶承载 P99 秩时，P99 整体省略而不是基于被截断的样本编造百分位。
    overflow_window = LatencyWindow(window_ms=WINDOW_MS)
    overflow_window.add(now, 10.0)  # 1 个正常样本
    for _ in range(99):  # 99 个 >1h 的极端延迟把第 99 秩推进溢出桶
        overflow_window.add(now, 3_600_001.0)
    overflow_bins = LatencyWindow.window_bins(
        (overflow_window.snapshot(),), now=now, window_ms=WINDOW_MS
    )
    assert LatencyWindow.percentile_bins(Counter(overflow_bins)) is None
    obs2 = Observability(window_ms=WINDOW_MS)
    obs2.sample({"delivered_total": 100}, process_latency_bins=overflow_bins)
    assert "write_latency_ms_p99" not in obs2.snapshot(WINDOW_MS)


def test_delivery_scope_excludes_process_readiness_metrics() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    # B2：Delivery 级 P99 由 Worker 侧直方图计算后经 IPC 上报，不再传原始观测
    obs.sample(
        {"delivered_total": 1},
        delivery_counters={"ems_power": {"delivered_total": 5, "failed_total": 0}},
        delivery_p99={"ems_power": 4.0},
    )
    details = obs.delivery_snapshot(WINDOW_MS)
    assert set(details) == {"ems_power"}
    scope = details["ems_power"]
    assert "metrics_snapshot_ready" not in scope
    assert "metrics_covered_ms" not in scope
    assert scope["delivered_total"] == 5.0
    assert scope["write_latency_ms_p99"] == 4.0


def test_event_ring_is_bounded_and_cursor_paged() -> None:
    obs = Observability(max_events=3, window_ms=WINDOW_MS)
    for code in ("E1", "E2", "E3", "E4"):
        obs.event("WARN", "ems_power", code, f"message {code}")
    page, token = obs.list_events(start=None, end=None, query="", after_id="", count=2)
    assert [item.error_code for item in page] == ["E2", "E3"]  # E1 evicted by ring bound
    assert token == page[-1].event_id
    next_page, next_token = obs.list_events(start=None, end=None, query="", after_id=token, count=2)
    assert [item.error_code for item in next_page] == ["E4"]
    assert next_token == ""  # last page


def test_event_cursor_rejects_unknown_id_and_filters_query_and_time() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    obs.event("ERROR", "reliable", "DLQ_01", "dead letter emitted")
    with pytest.raises(ValueError, match="cursor expired or invalid"):
        obs.list_events(start=None, end=None, query="", after_id="deadbeef", count=10)

    matched, _ = obs.list_events(start=None, end=None, query="DLQ", after_id="", count=10)
    assert len(matched) == 1
    no_match, _ = obs.list_events(start=None, end=None, query="redis", after_id="", count=10)
    assert no_match == ()

    future = datetime.now(UTC) + timedelta(hours=1)
    empty, _ = obs.list_events(start=future, end=None, query="", after_id="", count=10)
    assert empty == ()


def test_event_fields_are_truncated_to_contract_limits() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    obs.event("X" * 200, "S" * 200, "C" * 200, "M" * 2000)
    page, _ = obs.list_events(start=None, end=None, query="", after_id="", count=1)
    event = page[0]
    assert len(event.source) == 128
    assert len(event.error_code) == 128
    assert len(event.message) == 1024
