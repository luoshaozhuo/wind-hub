"""Bounded readiness check through the real CLI and the services' gRPC endpoints."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SERVICES = (
    "gateway",
    "data-generator",
    "collector_realtime",
    "collector_reliable",
    "sink",
    "commander",
)
HEALTHY = frozenset({"READY", "HEALTHY", "PASS"})


def _status(config: Path, service: str, timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "--config",
            str(config),
            "--format",
            "json",
            "service",
            "status",
            service,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    json_lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if not json_lines:
        raise RuntimeError(
            f"{service}: CLI returned {completed.returncode} without JSON envelope: "
            f"{completed.stderr[:300]}"
        )
    if completed.returncode != 0:
        raise RuntimeError(f"{service}: CLI status returned {completed.returncode}")
    response = json.loads(json_lines[-1])
    if not isinstance(response, dict) or response.get("status") != "SUCCESS":
        raise ValueError(f"{service}: CLI response is not SUCCESS")
    rows = response.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError(f"{service}: expected exactly one service status row")
    status = rows[0]
    if status.get("service") != service:
        raise ValueError(f"{service}: CLI returned a different service")
    return status


def _ready(state: dict[str, Any]) -> bool:
    business = state.get("business")
    if not isinstance(business, dict):
        return False
    if business.get("health", business.get("status")) not in HEALTHY:
        return False
    process = state.get("process")
    if not isinstance(process, dict):
        return False
    if process.get("status") == "UNMANAGED":
        return True
    if process.get("status") == "UNKNOWN" or process.get("running") is False:
        return False
    # 受管进程必须真实运行；RPC-only 配置只允许显式 UNMANAGED 豁免。
    return process.get("running") is True or process.get("state") == "RUNNING"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.timeout > 600:
        parser.error("--timeout must be in (0, 600]")
    deadline = time.monotonic() + args.timeout
    remaining = set(SERVICES)
    last: dict[str, Any] = {}
    while remaining and time.monotonic() < deadline:
        for service in tuple(sorted(remaining)):
            budget = min(10.0, max(0.2, deadline - time.monotonic()))
            try:
                state = _status(args.config, service, budget)
                last[service] = state
                if _ready(state):
                    remaining.remove(service)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                last[service] = {"error": str(exc)}
        if remaining:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    print(
        json.dumps(
            {
                "ready": not bool(remaining),
                "services": {name: last.get(name) for name in SERVICES},
                "pending": sorted(remaining),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
