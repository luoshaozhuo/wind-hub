"""汇总容量筛选实验矩阵。"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "run/exp-evidence")
rows = []
for f in sorted(root.glob("*.json")):
    if "metric" in f.name or "samples" in f.name:
        continue
    r = json.loads(f.read_text())
    ks = r.get("kafka_stage_diagnostics") or {}
    audit = r.get("device_order_audit") or {}
    dev_parts = audit.get("device_partitions") or {}
    rows.append({
        "file": f.name,
        "degree": r.get("degree"),
        "duration": r.get("duration_s"),
        "partitions": r.get("kafka_partition_count"),
        "batch": (r.get("kafka_capacity_profile") or {}).get("max_batch_size"),
        "publish": r.get("achieved_publish_batches_per_s"),
        "confirmed": r.get("confirmed_batches_per_s_steady"),
        "backlog_slope": r.get("backlog_slope_batches_per_s_steady"),
        "drained": r.get("drained"),
        "zero_loss": r.get("zero_loss"),
        "coalesced": r.get("cross_device_coalescing_observed"),
        "pts_per_builder": r.get("confirmed_points_per_physical_builder"),
        "submit_ms": (ks.get("submit") or {}).get("mean_duration_ms"),
        "confirm_ms": (ks.get("confirm") or {}).get("mean_duration_ms"),
        "lag_p99": r.get("event_loop_lag_p99_max_ms"),
        "cpu": r.get("cpu_percent_of_one_core"),
        "dlq": r.get("dlq_count"),
        "grpc_fail": r.get("grpc_failures"),
        "distinct_device_partition_sets": len({tuple(v) for v in dev_parts.values()}),
    })
print(json.dumps(rows, indent=2))
