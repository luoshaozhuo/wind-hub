"""Same-process before/after benchmark for the real taosws column factories.

This measures only column construction and bind-view creation. Statement bind and
execute are deliberately mocked so no network latency is attributed to columns.
Run from the repository root with the wind-hub Python environment.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import resource
import statistics
import time
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import taosws

from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "stmt2_before_after.json"
BEFORE = Path("/tmp/d5_wide_stmt2_before.py")
ITERATIONS = int(os.environ.get("D5_BENCHMARK_ITERATIONS", "300"))


def load_before() -> ModuleType:
    spec = importlib.util.spec_from_file_location("d5_wide_stmt2_before", BEFORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def main() -> None:
    from storage_service.infrastructure.adapters.outbound import wide_stmt2 as after

    before = load_before()
    schema = WideSchema.build(
        point_table_id="point_table_d5",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=tuple(PhysicalPoint(f"p{i:03d}", "FLOAT") for i in range(500)),
        max_row_bytes=65536,
    )
    devices = tuple(f"CAP_WTG{i:03d}" for i in range(68))
    records = tuple(
        WideRecord(
            wind_farm_id="e2e-final",
            device_config_id="device_d5",
            device_id=devices[index % len(devices)],
            data_level=WideDataLevel.PROTOCOL,
            sample_time_us=1_800_000_000_000_000 + index * 100_000,
            point_ids=schema.point_ids,
            values=tuple(float(index * 1000 + point) + 0.25 for point in range(500)),
            quality_bitmap=bytes([0x55]) * 125,
        )
        for index in range(72)
    )
    device_types = {device: "wind_turbine" for device in devices}

    def invoke(module: ModuleType) -> dict[str, float]:
        statement = Mock()
        statement.execute.return_value = len(records)
        stages: dict[str, float] = {}
        module.write_wide_stmt2(
            Mock(),
            taosws,
            schema=schema,
            records=records,
            device_types=device_types,
            statement=statement,
            stage_seconds=stages,
        )
        assert statement.bind.call_count == 1
        assert len(statement.bind.call_args.args[0]) == len(devices)
        return stages

    # Warm native bindings and both Python paths before collecting samples.
    for module in (before, after):
        for _ in range(20):
            invoke(module)

    samples: dict[str, list[dict[str, float]]] = {"before": [], "after": []}
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for iteration in range(ITERATIONS):
        order = (("before", before), ("after", after))
        if iteration % 2:
            order = tuple(reversed(order))
        for name, module in order:
            started = time.perf_counter()
            stages = invoke(module)
            elapsed = time.perf_counter() - started
            samples[name].append({"total_s": elapsed, **stages})
    wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    result: dict[str, Any] = {
        "git_head": os.popen("git rev-parse HEAD").read().strip(),
        "iterations_per_implementation": ITERATIONS,
        "alternating_order": "AB on even iterations, BA on odd iterations",
        "rows_per_execute": len(records),
        "device_bindings_per_execute": len(devices),
        "points_per_row": len(schema.columns),
        "scope": "real taosws typed column construction and bind-param views; bind/execute mocked",
        "overall_wall_seconds": wall_seconds,
        "overall_process_cpu_seconds": cpu_seconds,
        "overall_cpu_percent_of_one_core": 100 * cpu_seconds / wall_seconds,
        "max_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "implementations": {},
    }
    for name, observations in samples.items():
        summary: dict[str, Any] = {}
        for stage in ("total_s", "columns", "bind", "execute"):
            values = [observation[stage] * 1000 for observation in observations]
            summary[stage.removesuffix("_s") + "_ms"] = {
                "mean": statistics.fmean(values),
                "p95": percentile(values, 0.95),
                "p99": percentile(values, 0.99),
                "max": max(values),
            }
        mean_total_s = statistics.fmean(item["total_s"] for item in observations)
        summary["rows_per_second"] = len(records) / mean_total_s
        result["implementations"][name] = summary
    before_mean = result["implementations"]["before"]["columns_ms"]["mean"]
    after_mean = result["implementations"]["after"]["columns_ms"]["mean"]
    result["columns_mean_reduction_percent"] = 100 * (before_mean - after_mean) / before_mean
    result["rows_per_second_change_percent"] = 100 * (
        result["implementations"]["after"]["rows_per_second"]
        / result["implementations"]["before"]["rows_per_second"]
        - 1
    )
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
