"""压测报告生成（决策 8）：Markdown 主报告 + JSON 原始数据 + 可选 PNG 曲线。

报告内容（决策 8）：摘要（环境/日期）→ 协议 × 场景矩阵表 → 延迟分布
（P50/P95/P99 表 + 可选曲线图）→ 重连行为 → 资源占用 → 自动结论。

matplotlib 是可选依赖：缺失时 ``render_latency_curve`` 返回 ``False``
并在 Markdown 中保留表格（曲线小节标注「未生成」），不报错。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from tests.perf.collector import PerfMetrics, _percentile

logger = logging.getLogger(__name__)


def aggregate_runs(runs: list[PerfMetrics]) -> PerfMetrics:
    """把同一 (protocol, scenario) 的 N 次运行聚合为单条结果（step24）。

    口径：吞吐 / 资源占用 / 点计数取均值；延迟**合并全部原始样本**后
    重算百分位（不是对百分位取均值——后者在数学上不成立）；重连次数
    求和，最长恢复取各次最大值。

    Raises:
        ValueError: 空列表，或混入不同协议 / 场景的结果。
    """
    if not runs:
        raise ValueError("aggregate_runs 需要至少一次运行结果")
    if len({(m.protocol, m.scenario) for m in runs}) != 1:
        raise ValueError("aggregate_runs 只能聚合同一 (protocol, scenario) 的多次运行")
    first = runs[0]
    n = len(runs)
    lat = sorted(s for m in runs for s in m.latency_samples_ms)
    return PerfMetrics(
        protocol=first.protocol,
        scenario=first.scenario,
        duration_s=sum(m.duration_s for m in runs) / n,
        points_collected=round(sum(m.points_collected for m in runs) / n),
        points_routed=round(sum(m.points_routed for m in runs) / n),
        points_dropped=round(sum(m.points_dropped for m in runs) / n),
        throughput_pps=sum(m.throughput_pps for m in runs) / n,
        latency_p50_ms=_percentile(lat, 50),
        latency_p95_ms=_percentile(lat, 95),
        latency_p99_ms=_percentile(lat, 99),
        latency_max_ms=max(m.latency_max_ms for m in runs),
        cpu_percent=sum(m.cpu_percent for m in runs) / n,
        memory_mb=sum(m.memory_mb for m in runs) / n,
        fd_count=round(sum(m.fd_count for m in runs) / n),
        reconnect_count=sum(m.reconnect_count for m in runs),
        reconnect_time_ms=max(m.reconnect_time_ms for m in runs),
        latency_samples_ms=lat,
    )


def generate_markdown_report(
    results: list[PerfMetrics],
    output_path: Path,
    title: str = "wind-hub Performance Benchmark",
    runs: int = 1,
) -> None:
    """生成 Markdown 主报告（人看）。空结果也会生成带说明的骨架报告。

    ``runs`` > 1 时结果视为 :func:`aggregate_runs` 的聚合产物，摘要节
    会标注聚合口径。
    """
    lines: list[str] = [f"# {title}", ""]
    lines += _summary_section(results, runs)
    if not results:
        lines += ["", "> ⚠️ 本次压测没有任何结果（可能全部场景失败或被跳过）。", ""]
    else:
        lines += _matrix_section(results)
        lines += _latency_section(results)
        lines += _reconnect_section(results)
        lines += _resource_section(results)
        lines += _conclusion_section(results)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Markdown 报告已写入 %s（%d 条结果）", output_path, len(results))


def generate_json_report(results: list[PerfMetrics], output_path: Path) -> None:
    """生成 JSON 原始数据报告（机器消费）。"""
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": _environment(),
        "results": [m.to_dict() for m in results],
    }
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("JSON 报告已写入 %s（%d 条结果）", output_path, len(results))


def render_latency_curve(results: list[PerfMetrics], output_path: Path) -> bool:
    """渲染延迟曲线 PNG（每协议一条，X 轴场景，Y 轴 P50/P95/P99）。

    Returns:
        ``True`` 生成成功；``False`` matplotlib 不可用或无数据（跳过）。
    """
    if not results:
        return False
    try:
        import matplotlib

        matplotlib.use("Agg")  # 无显示环境（CI/WSL）强制非交互后端
        import matplotlib.pyplot as plt
    except ImportError:
        logger.info("matplotlib 不可用，跳过延迟曲线 PNG（表格仍在 Markdown 中）")
        return False

    protocols = sorted({m.protocol for m in results})
    fig, ax = plt.subplots(figsize=(10, 6))
    for protocol in protocols:
        rows = [m for m in results if m.protocol == protocol]
        scenarios = [m.scenario for m in rows]
        ax.plot(scenarios, [m.latency_p50_ms for m in rows], marker="o", label=f"{protocol} P50")
        ax.plot(scenarios, [m.latency_p95_ms for m in rows], marker="s", label=f"{protocol} P95")
        ax.plot(scenarios, [m.latency_p99_ms for m in rows], marker="^", label=f"{protocol} P99")
    ax.set_xlabel("Scenario")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("Latency distribution by scenario")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    logger.info("延迟曲线已写入 %s", output_path)
    return True


# ---------------------------------------------------------------------------
# 各小节
# ---------------------------------------------------------------------------


def _environment() -> dict[str, str | int]:
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count() or 0,
    }


def _summary_section(results: list[PerfMetrics], runs: int = 1) -> list[str]:
    env = _environment()
    protocols = sorted({m.protocol for m in results})
    scenarios = sorted({m.scenario for m in results})
    duration = max((m.duration_s for m in results), default=0.0)
    lines = [
        "## 1. 摘要",
        "",
        f"- 日期：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- 平台：{env['platform']}",
        f"- Python：{env['python']}（{env['cpu_count']} 核）",
        f"- 协议：{', '.join(protocols) if protocols else '(无)'}",
        f"- 场景数：{len(scenarios)}（{', '.join(scenarios) if scenarios else '(无)'}）",
        f"- 单场景测量时长：{duration:g}s（另有预热，不计入统计）",
        "- Sink：NullSink（隔离外部 IO，测采集 + Pipeline + Router）",
    ]
    if runs > 1:
        lines.append(
            f"- 每场景重复 {runs} 次（{runs} runs, aggregated）：吞吐/资源取均值，"
            "延迟合并全部样本重算百分位，重连次数求和"
        )
    return lines


def _matrix_section(results: list[PerfMetrics]) -> list[str]:
    lines = [
        "",
        "## 2. 性能矩阵（协议 × 场景）",
        "",
        "| 协议 | 场景 | 吞吐 (点/s) | P50 (ms) | P95 (ms) | P99 (ms) | 最大 (ms) | 丢点 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in results:
        lines.append(
            f"| {m.protocol} | {m.scenario} | {m.throughput_pps:.0f} "
            f"| {m.latency_p50_ms:.2f} | {m.latency_p95_ms:.2f} | {m.latency_p99_ms:.2f} "
            f"| {m.latency_max_ms:.2f} | {m.points_dropped} |"
        )
    return lines


def _latency_section(results: list[PerfMetrics]) -> list[str]:
    lines = [
        "",
        "## 3. 延迟分布",
        "",
        "单点延迟 = 采集时间戳 → pipeline 处理完成（engine observer 口径）。",
        "",
        "| 协议 | 场景 | P50 (ms) | P95 (ms) | P99 (ms) | 最大 (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for m in results:
        lines.append(
            f"| {m.protocol} | {m.scenario} | {m.latency_p50_ms:.2f} "
            f"| {m.latency_p95_ms:.2f} | {m.latency_p99_ms:.2f} | {m.latency_max_ms:.2f} |"
        )
    lines += ["", "曲线图：`perf_latency.png`（matplotlib 可用时生成）。"]
    return lines


def _reconnect_section(results: list[PerfMetrics]) -> list[str]:
    lines = [
        "",
        "## 4. 重连行为",
        "",
        "以驱动 ``health()`` 每秒轮询的「健康 → 不健康 → 健康」跳变计数。",
        "",
        "| 协议 | 场景 | 重连次数 | 最长恢复 (ms) |",
        "|---|---|---|---|",
    ]
    for m in results:
        lines.append(
            f"| {m.protocol} | {m.scenario} | {m.reconnect_count} | {m.reconnect_time_ms:.0f} |"
        )
    return lines


def _resource_section(results: list[PerfMetrics]) -> list[str]:
    lines = [
        "",
        "## 5. 资源占用（wind-hub 进程）",
        "",
        "CPU 为测量窗口平均利用率（单核 100% 上限）；内存/FD 为窗口峰值。",
        "",
        "| 协议 | 场景 | CPU (%) | 峰值内存 (MiB) | 峰值 FD |",
        "|---|---|---|---|---|",
    ]
    for m in results:
        lines.append(
            f"| {m.protocol} | {m.scenario} | {m.cpu_percent:.1f} "
            f"| {m.memory_mb:.1f} | {m.fd_count} |"
        )
    return lines


def _conclusion_section(results: list[PerfMetrics]) -> list[str]:
    """基于阈值的自动观察（保守措辞，只陈述数据）。"""
    observations: list[str] = []
    ideal = [m for m in results if m.scenario == "ideal"]
    for m in ideal:
        observations.append(
            f"- 理想链路下 {m.protocol} 吞吐 {m.throughput_pps:.0f} 点/s，"
            f"P99 延迟 {m.latency_p99_ms:.2f} ms。"
        )
    outages = [m for m in results if m.reconnect_count > 0]
    if outages:
        worst = max(outages, key=lambda m: m.reconnect_time_ms)
        observations.append(
            f"- 中断场景下共记录 {sum(m.reconnect_count for m in outages)} 次重连，"
            f"最长恢复 {worst.reconnect_time_ms:.0f} ms（{worst.protocol} / {worst.scenario}）。"
        )
    dropped = [m for m in results if m.points_dropped > 0]
    if dropped:
        worst_drop = max(dropped, key=lambda m: m.points_dropped)
        observations.append(
            f"- {len(dropped)} 个场景出现丢点，最严重为 {worst_drop.protocol} / "
            f"{worst_drop.scenario}（{worst_drop.points_dropped} 点）——检查背压配置。"
        )
    else:
        observations.append("- 所有场景无丢点。")
    return ["", "## 6. 结论与建议", ""] + observations + [""]
