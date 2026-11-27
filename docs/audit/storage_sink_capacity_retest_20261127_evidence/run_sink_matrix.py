"""Run the six legal Sink parameter cases sequentially."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs" / "audit" / "storage_sink_capacity_retest_20261127_evidence"


def main() -> None:
    summary = []
    for partitions in (1, 4):
        for inflight in (16, 32, 64):
            name = f"p{partitions}_c{inflight}"
            directory = EVIDENCE / "sink_matrix" / name
            directory.mkdir(parents=True, exist_ok=True)
            env = {
                **os.environ,
                "R14_DURATION_S": "60",
                "R14_DEGREE": "30",
                "R14_TARGET_BATCH_RATE": "300",
                "R14_POINTS": "500",
                "R14_KAFKA_PARTITIONS": str(partitions),
                "R14_KAFKA_MAX_SAFE_INFLIGHT": str(inflight),
                "R14_KAFKA_LINGER_MS": "10",
                "R14_RESULT_DIR": str(directory),
            }
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/sink_service/system/test_capacity_r14_sustained.py",
                f"--junitxml={directory / 'capacity.xml'}",
            ]
            started = time.monotonic()
            with (directory / "capacity.log").open("w") as stream:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                    check=False,
                )
            results = [
                str(path.relative_to(ROOT))
                for path in directory.glob("r14_capacity_*.json")
                if not path.name.endswith("_metric_names.json")
            ]
            summary.append(
                {
                    "case": name,
                    "partitions": partitions,
                    "max_safe_inflight": inflight,
                    "returncode": completed.returncode,
                    "elapsed_seconds": time.monotonic() - started,
                    "result_files": results,
                }
            )
            (EVIDENCE / "sink_matrix_summary.json").write_text(json.dumps(summary, indent=2))
            print(json.dumps(summary[-1]), flush=True)
    if any(item["returncode"] for item in summary):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
