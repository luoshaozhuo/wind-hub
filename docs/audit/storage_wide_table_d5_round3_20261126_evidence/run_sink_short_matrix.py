"""Run D5 round-three short Sink matrix sequentially with raw evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs" / "audit" / "storage_wide_table_d5_round3_20261126_evidence"
TEST = "tests/sink_service/system/test_capacity_r14_sustained.py"


def main() -> None:
    summary: list[dict[str, object]] = []
    for concurrency in (8, 16, 32):
        for linger in (0, 1, 5, 10):
            name = f"c{concurrency}_l{linger}"
            directory = EVIDENCE / "sink_short_matrix" / name
            directory.mkdir(parents=True, exist_ok=True)
            env = {
                **os.environ,
                "R14_DURATION_S": "60",
                "R14_DEGREE": "30",
                "R14_TARGET_BATCH_RATE": "300",
                "R14_POINTS": "500",
                "R14_MIN_SUSTAINED_FRACTION": "0.95",
                "R14_KAFKA_MAX_SAFE_INFLIGHT": str(concurrency),
                "R14_KAFKA_LINGER_MS": str(linger),
                "R14_RESULT_DIR": str(directory),
            }
            log = directory / "capacity.log"
            xml = directory / "capacity.xml"
            command = [sys.executable, "-m", "pytest", "-q", TEST, f"--junitxml={xml}"]
            started = time.monotonic()
            with log.open("w", encoding="utf-8") as stream:
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
                for path in directory.glob("r14_*.json")
                if not path.name.endswith("_metric_names.json")
            ]
            summary.append(
                {
                    "case": name,
                    "concurrency": concurrency,
                    "linger_ms": linger,
                    "returncode": completed.returncode,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "log": str(log.relative_to(ROOT)),
                    "junit": str(xml.relative_to(ROOT)),
                    "result_files": results,
                }
            )
            (EVIDENCE / "sink_short_matrix_summary.json").write_text(
                json.dumps(summary, indent=2)
            )
            print(json.dumps(summary[-1]), flush=True)
    if any(item["returncode"] for item in summary):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
