"""Unit tests for ``tests/perf/reporter.py`` — 报告生成。

只写临时文件，不依赖 matplotlib（缺失时曲线返回 False 也是预期路径）。
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from tests.performance.collector import PerfMetrics
from tests.performance.reporter import (
    aggregate_runs,
    generate_json_report,
    generate_markdown_report,
    render_latency_curve,
)


def _metric(
    protocol: str = "modbus",
    scenario: str = "ideal",
    **overrides: Any,
) -> PerfMetrics:
    values: dict[str, Any] = {
        "protocol": protocol,
        "scenario": scenario,
        "duration_s": 60.0,
        "points_collected": 600000,
        "points_routed": 600000,
        "points_dropped": 0,
        "throughput_pps": 10000.0,
        "latency_p50_ms": 1.5,
        "latency_p95_ms": 4.0,
        "latency_p99_ms": 8.0,
        "latency_max_ms": 30.0,
        "cpu_percent": 35.0,
        "memory_mb": 180.0,
        "fd_count": 64,
        "reconnect_count": 0,
        "reconnect_time_ms": 0.0,
    }
    values.update(overrides)
    return PerfMetrics(**values)


def test_markdown_report_contains_all_sections(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    generate_markdown_report(
        [_metric(), _metric(protocol="iec104", scenario="loss_1pct", reconnect_count=1)],
        out,
    )
    text = out.read_text(encoding="utf-8")

    assert "## 1. 摘要" in text
    assert "## 2. 性能矩阵（协议 × 场景）" in text
    assert "## 3. 延迟分布" in text
    assert "## 4. 重连行为" in text
    assert "## 5. 资源占用（wind-hub 进程）" in text
    assert "## 6. 结论与建议" in text
    # 矩阵行内容
    assert "| modbus | ideal | 10000 |" in text
    assert "| iec104 | loss_1pct |" in text


def test_markdown_report_empty_results(tmp_path: Path) -> None:
    out = tmp_path / "empty.md"
    generate_markdown_report([], out)
    text = out.read_text(encoding="utf-8")
    assert "## 1. 摘要" in text
    assert "没有任何结果" in text


def test_json_report_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    metrics = [_metric(), _metric(protocol="ads", scenario="delay_50ms")]
    generate_json_report(metrics, out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["environment"]["python"]
    assert len(payload["results"]) == 2
    row = payload["results"][0]
    assert row["protocol"] == "modbus"
    assert row["throughput_pps"] == 10000.0
    assert row["latency_p99_ms"] == 8.0


def test_json_report_empty_results(tmp_path: Path) -> None:
    out = tmp_path / "empty.json"
    generate_json_report([], out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["results"] == []


def test_render_latency_curve_empty_results(tmp_path: Path) -> None:
    assert render_latency_curve([], tmp_path / "curve.png") is False


# ---------------------------------------------------------------------------
# aggregate_runs（step24：--runs N 聚合）
# ---------------------------------------------------------------------------


class TestAggregateRuns:
    def test_throughput_and_resources_averaged(self) -> None:
        m1 = _metric(throughput_pps=9000.0, cpu_percent=30.0, memory_mb=150.0, fd_count=60)
        m2 = _metric(throughput_pps=11000.0, cpu_percent=40.0, memory_mb=170.0, fd_count=70)
        agg = aggregate_runs([m1, m2])

        assert agg.throughput_pps == 10000.0
        assert agg.cpu_percent == 35.0
        assert agg.memory_mb == 160.0
        assert agg.fd_count == 65

    def test_latency_samples_merged_and_percentiles_recomputed(self) -> None:
        m1 = _metric(latency_samples_ms=[1.0, 2.0, 3.0, 4.0], latency_max_ms=4.0)
        m2 = _metric(latency_samples_ms=[5.0, 6.0, 7.0, 8.0], latency_max_ms=8.0)
        agg = aggregate_runs([m1, m2])

        # 合并后样本 [1..8]：P50 = 4，P99 最近秩 = 8；max 取各次最大
        assert agg.latency_samples_ms == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        assert agg.latency_p50_ms == 4.0
        assert agg.latency_p99_ms == 8.0
        assert agg.latency_max_ms == 8.0

    def test_reconnect_count_summed(self) -> None:
        m1 = _metric(reconnect_count=2, reconnect_time_ms=1500.0)
        m2 = _metric(reconnect_count=1, reconnect_time_ms=3000.0)
        agg = aggregate_runs([m1, m2])

        assert agg.reconnect_count == 3
        assert agg.reconnect_time_ms == 3000.0

    def test_rejects_mixed_scenarios(self) -> None:
        with pytest.raises(ValueError, match="同一"):
            aggregate_runs([_metric(), _metric(scenario="loss_1pct")])

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="至少"):
            aggregate_runs([])


def test_markdown_report_marks_aggregated_runs(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    generate_markdown_report([_metric()], out, runs=3)
    text = out.read_text(encoding="utf-8")
    assert "3 runs, aggregated" in text


def test_render_latency_curve_without_matplotlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """matplotlib 缺失时返回 False 而非报错（可选依赖语义）。"""
    real_import = builtins.__import__

    def _no_matplotlib(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("matplotlib"):
            raise ImportError("no matplotlib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_matplotlib)
    assert render_latency_curve([_metric()], tmp_path / "curve.png") is False
