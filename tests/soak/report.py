"""soak 验收报告生成：Markdown 主报告 + JSON 原始数据。

复用 perf reporter 的环境摘要（``_environment``），口径对齐 perf 报告：
摘要 → 负载形态与数据完整性 → 各任务节拍（间隔/抖动/错过周期）→
sink 行为（收到/重复/延迟）→ 重连 → 资源占用 → 结论。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from tests.collector.perf.reporter import _environment
from tests.soak.metrics import SoakMetrics

logger = logging.getLogger(__name__)


def generate_soak_markdown(
    result: SoakMetrics,
    output_path: Path,
    *,
    title: str | None = None,
) -> None:
    """生成单次 soak 运行的 Markdown 报告。"""
    lines: list[str] = [f"# {title or f'wind-hub Soak — {result.profile}'}", ""]
    lines += _summary_section(result)
    lines += _flow_section(result)
    lines += _cycle_section(result)
    lines += _sink_section(result)
    if result.reconnect_count or result.profile == "reconnect_storm":
        lines += _reconnect_section(result)
    if result.write_commands:
        lines += _write_section(result)
    lines += _resource_section(result)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("soak Markdown 报告已写入 %s", output_path)


def generate_soak_json(result: SoakMetrics, output_path: Path) -> None:
    """生成 JSON 原始数据报告（机器消费）。"""
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": _environment(),
        "result": result.to_dict(),
    }
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("soak JSON 报告已写入 %s", output_path)


def _summary_section(result: SoakMetrics) -> list[str]:
    env = _environment()
    return [
        "## 摘要",
        "",
        f"- 负载形态：`{result.profile}`",
        f"- 测量时长：{result.duration_s:g}s",
        f"- 生成时间：{datetime.now(UTC).isoformat()}",
        f"- 环境：{env['platform']} / Python {env['python']} / {env['cpu_count']} CPU",
        "",
    ]


def _flow_section(result: SoakMetrics) -> list[str]:
    loss = result.points_collected - result.sink_received
    return [
        "## 数据完整性",
        "",
        "| 采集 | 路由 | 背压丢弃 | sink 收到 | 端到端丢失 | 吞吐 (点/s) |",
        "|---|---|---|---|---|---|",
        f"| {result.points_collected} | {result.points_routed} | {result.points_dropped} "
        f"| {result.sink_received} | {loss} | {result.throughput_pps:.1f} |",
        "",
    ]


def _cycle_section(result: SoakMetrics) -> list[str]:
    lines = [
        "## 采集节拍",
        "",
        "| 任务 | 目标间隔 (s) | 周期数 | 平均间隔 (s) | P99 间隔 (s) | 抖动 σ (s) | 错过周期 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in result.cycles:
        lines.append(
            f"| {c.task_id} | {c.target_interval_s:g} | {c.cycles} "
            f"| {c.interval_mean_s:.4f} | {c.interval_p99_s:.4f} "
            f"| {c.jitter_s:.4f} | {c.missed_cycles} |"
        )
    lines.append("")
    return lines


def _sink_section(result: SoakMetrics) -> list[str]:
    return [
        "## Sink 行为",
        "",
        "| 收到点数 | 重复点数 | 延迟 P50 (ms) | 延迟 P99 (ms) |",
        "|---|---|---|---|",
        f"| {result.sink_received} | {result.sink_duplicates} "
        f"| {result.sink_latency_p50_ms:.1f} | {result.sink_latency_p99_ms:.1f} |",
        "",
    ]


def _reconnect_section(result: SoakMetrics) -> list[str]:
    return [
        "## 重连行为",
        "",
        f"- 重连次数：{result.reconnect_count}",
        f"- 最长恢复时间：{result.reconnect_time_ms:.0f} ms",
        "",
    ]


def _write_section(result: SoakMetrics) -> list[str]:
    return [
        "## 写指令",
        "",
        f"- 写命令数：{result.write_commands}",
        f"- 失败数：{result.write_failures}",
        "",
    ]


def _resource_section(result: SoakMetrics) -> list[str]:
    return [
        "## 资源占用",
        "",
        "| CPU (%) | 内存峰值 (MB) | FD 峰值 | 队列深度峰值 | asyncio 任务数峰值 |",
        "|---|---|---|---|---|",
        f"| {result.cpu_percent:.1f} | {result.memory_mb:.1f} | {result.fd_count} "
        f"| {result.queue_depth_max} | {result.asyncio_tasks_max} |",
        "",
    ]
