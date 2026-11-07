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

from datetime import UTC, datetime, timedelta

import pytest

from sink_service.application.observability import Observability

pytestmark = [pytest.mark.component, pytest.mark.contract]

WINDOW_MS = 60000
# The exact process metric vocabulary: twelve R04 names (design R04 / 9.3.2)
# plus the eight batch-v2 counters emitted by the Realtime/Reliable workers.
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
    "received_batches_total",
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
    obs.sample({"delivered_total": 1})  # no latency observations
    assert "write_latency_ms_p99" not in obs.snapshot(WINDOW_MS)

    # Nearest-rank P99: index ceil(N * 0.99) - 1 = 98 over the ordered sample.
    obs2 = Observability(window_ms=WINDOW_MS)
    now = datetime.now(UTC)
    latencies = [(now - timedelta(milliseconds=i), float(i)) for i in range(100)]
    obs2.sample({"delivered_total": 100}, latency_observations=latencies)
    ordered = sorted(value for _, value in latencies)
    assert obs2.snapshot(WINDOW_MS)["write_latency_ms_p99"] == ordered[98]


def test_latency_window_is_half_open_and_overflow_suppresses_p99() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    now = datetime.now(UTC)
    inside = [(now - timedelta(seconds=5), 10.0)]
    outside = [(now - timedelta(seconds=120), 999.0)]  # older than the 60s cutoff
    obs.sample({"delivered_total": 1}, latency_observations=[*outside, *inside])
    assert obs.snapshot(WINDOW_MS)["write_latency_ms_p99"] == 10.0

    # A declared overflow whose instant is still inside the window invalidates
    # P99 rather than reporting a percentile over a truncated sample.
    obs2 = Observability(window_ms=WINDOW_MS)
    obs2.sample(
        {"delivered_total": 1},
        latency_observations=inside,
        latency_overflow_at=now - timedelta(seconds=1),
    )
    assert "write_latency_ms_p99" not in obs2.snapshot(WINDOW_MS)


def test_delivery_scope_excludes_process_readiness_metrics() -> None:
    obs = Observability(window_ms=WINDOW_MS)
    now = datetime.now(UTC)
    obs.sample(
        {"delivered_total": 1},
        delivery_counters={"ems_power": {"delivered_total": 5, "failed_total": 0}},
        delivery_latencies={"ems_power": [(now, 4.0)]},
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
