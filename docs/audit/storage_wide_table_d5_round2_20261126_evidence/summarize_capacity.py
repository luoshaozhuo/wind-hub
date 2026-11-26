"""Extract comparable D5 capacity metrics without changing raw results."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def r14(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text())
    audit = data.get("duplicate_audit", {})
    return {
        "path": path.name,
        "degree": data["degree"],
        "target_rows_per_second": data["target_batch_rate_global"],
        "confirmed_rows_per_second": data["confirmed_batches_per_s_steady"],
        "confirmed_points_per_second": data["confirmed_values_per_s_steady"],
        "confirmed_mb_per_second": data.get("confirmed_payload_mb_per_s_end_to_end"),
        "published_rows": data["published_batches"],
        "confirmed_rows": data["confirmed_values"] // data["points_per_batch"],
        "backlog_slope_rows_per_second": data["backlog_slope_batches_per_s_steady"],
        "drained": data["drained"],
        "zero_loss": data["zero_loss"],
        "identity_closed": data.get("identity_closed"),
        "audit_total_records": audit.get("audit_total_records"),
        "audit_distinct_identities": audit.get("audit_distinct_identities"),
        "audit_duplicate_identities": audit.get("audit_duplicate_identities"),
        "cpu_percent_of_one_core": data["cpu_percent_of_one_core"],
        "rss_max_mib": data["rss_max_mib"],
        "grpc_p95_ms": data.get("grpc_probe_p95_ms"),
        "grpc_p99_ms": data.get("grpc_probe_p99_ms"),
        "write_window_p99_max_ms": data["write_latency_p99_max_ms"],
        "dlq": data["dlq_count"],
        "kafka_capacity_profile": data["kafka_capacity_profile"],
        "reliable_stage_diagnostics": data["reliable_stage_diagnostics"],
        "kafka_stage_diagnostics": data["kafka_stage_diagnostics"],
        "quota_stage_diagnostics": data["quota_stage_diagnostics"],
    }


def main() -> None:
    output: dict[str, object] = {"baseline": [], "matrix": [], "storage": []}
    for path in sorted(ROOT.glob("r14_capacity_*.json")):
        if path.name.endswith("_metric_names.json"):
            continue
        output["baseline"].append(r14(path))
    matrix_status = {
        item["case"]: item
        for item in json.loads((ROOT / "matrix_summary.json").read_text())
    } if (ROOT / "matrix_summary.json").exists() else {}
    for directory in sorted((ROOT / "matrix").glob("c*_l*")):
        if not directory.is_dir():
            continue
        for path in directory.glob("r14_capacity_*.json"):
            if path.name.endswith("_metric_names.json"):
                continue
            item = r14(path)
            status = matrix_status.get(directory.name, {})
            item["returncode"] = status.get("returncode")
            output["matrix"].append(item)
    for directory in sorted(ROOT.glob("storage_wide_*")):
        if not directory.is_dir():
            continue
        for path in directory.glob("storage_wide_500_devices_*.json"):
            data = json.loads(path.read_text())
            output["storage"].append(
                {
                    "path": str(path.relative_to(ROOT)),
                    "device_count": data["device_count"],
                    "cycles_per_device": data["cycles_per_device"],
                    "expected_rows": data["expected_rows"],
                    "published_rows_per_second": data["published_rows_per_second"],
                    "published_points_per_second": data["published_points_per_second"],
                    "published_mb_per_second": data["published_payload_MB_per_second"],
                    "end_to_end_rows_per_second": data["end_to_end_confirmed_rows_per_second"],
                    "end_to_end_points_per_second": data["end_to_end_confirmed_points_per_second"],
                    "drain_seconds": data["drain_seconds"],
                    "fully_persisted": data["fully_persisted"],
                    "blocked_partitions": data["blocked_partitions"],
                    "kafka": data["kafka"],
                    "storage_process": data["storage_process"],
                    "edge_verification": data["edge_verification"],
                }
            )
    (ROOT / "capacity_summary.json").write_text(json.dumps(output, indent=2))
    print(json.dumps({key: len(value) for key, value in output.items()}))


if __name__ == "__main__":
    main()
