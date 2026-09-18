#!/usr/bin/env python
"""独立压测入口（决策 9/10）——三协议 × tc netem 场景 × Markdown/JSON 报告。

用法（需要 root，因为 tc/ip 命令）::

    sudo /home/luo/miniconda3/envs/wind-hub/bin/python scripts/run_benchmark.py \
        --quick --protocol modbus

完整矩阵（3 协议 × 8 场景 × 60s + 预热，约半小时）::

    sudo .../python scripts/run_benchmark.py

无 root 时脚本检测权限后直接报告退出（决策 10：不自动 sudo）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 让 `tests.perf` / `tests.fixtures` 在 pytest 之外也可导入（仓库根入路径）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.perf.collector import PerfMetrics  # noqa: E402
from tests.perf.reporter import (  # noqa: E402
    aggregate_runs,
    generate_json_report,
    generate_markdown_report,
    render_latency_curve,
)
from tests.perf.runner import DEFAULT_PORTS, run_benchmark  # noqa: E402
from tests.perf.scenarios import CORE_SCENARIOS, QUICK_SCENARIOS  # noqa: E402
from tests.perf.veth import VethManager  # noqa: E402

logger = logging.getLogger("run_benchmark")


async def main() -> int:
    parser = argparse.ArgumentParser(description="wind-hub 三协议性能压测")
    parser.add_argument(
        "--protocol",
        choices=["modbus", "iec104", "ads", "all"],
        default="all",
        help="压测协议（默认全部）",
    )
    parser.add_argument("--quick", action="store_true", help="快速模式（3 场景 × 30 秒）")
    parser.add_argument("--duration", type=float, default=60.0, help="单场景测量时长（秒）")
    parser.add_argument("--warmup", type=float, default=10.0, help="预热时长（秒，不计入统计）")
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="每（协议, 场景）重复次数，>1 时聚合（吞吐/资源均值，延迟样本合并，重连求和）",
    )
    parser.add_argument("--output", default="perf_report.md", help="Markdown 报告路径")
    args = parser.parse_args()
    if args.runs < 1:
        print("错误：--runs 必须 >= 1")
        return 2

    veth = VethManager()
    if not veth.check_permission():
        print("错误：压测需要 root 权限（tc netem / ip link），请用 sudo 运行。")
        print("决策 10：脚本不会自动 sudo。")
        return 2

    protocols = ["modbus", "iec104", "ads"] if args.protocol == "all" else [args.protocol]
    scenarios = QUICK_SCENARIOS if args.quick else CORE_SCENARIOS
    duration = 30.0 if args.quick else args.duration

    results: list[PerfMetrics] = []
    async with veth.active() as pair:
        netem_devices = [pair.name_client, pair.name_server]
        for protocol in protocols:
            for scenario in scenarios:
                print(f"=== {protocol} / {scenario.name} ===", flush=True)
                run_results: list[PerfMetrics] = []
                for run_idx in range(1, args.runs + 1):
                    try:
                        metrics = await run_benchmark(
                            protocol=protocol,
                            scenario=scenario,
                            host=pair.ip_server,
                            port=DEFAULT_PORTS[protocol],
                            duration_s=duration,
                            warmup_s=args.warmup,
                            netem_devices=netem_devices,
                        )
                    except Exception:
                        logger.exception(
                            "场景失败：%s / %s（第 %d 次，跳过本次）",
                            protocol,
                            scenario.name,
                            run_idx,
                        )
                        continue
                    run_results.append(metrics)
                    if args.runs > 1:
                        print(
                            f"  run {run_idx}: 吞吐 {metrics.throughput_pps:.0f} 点/s | "
                            f"P99 {metrics.latency_p99_ms:.2f}ms",
                            flush=True,
                        )
                if not run_results:
                    continue
                metrics = aggregate_runs(run_results) if len(run_results) > 1 else run_results[0]
                results.append(metrics)
                print(
                    f"  吞吐 {metrics.throughput_pps:.0f} 点/s | "
                    f"P50 {metrics.latency_p50_ms:.2f}ms P99 {metrics.latency_p99_ms:.2f}ms | "
                    f"丢点 {metrics.points_dropped} | 重连 {metrics.reconnect_count}",
                    flush=True,
                )

    output = Path(args.output)
    generate_markdown_report(results, output, runs=args.runs)
    generate_json_report(results, output.with_suffix(".json"))
    render_latency_curve(results, output.with_name("perf_latency.png"))
    print(f"\n报告：{output} / {output.with_suffix('.json')}")
    return 0 if results else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))
