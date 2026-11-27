"""Preflight the released DCEMS Server assets without starting any service."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / "third_party" / "dc-ems"
CHECK = Literal["READY", "MISSING", "UNVERIFIED"]


def _release_version(path: Path) -> str:
    """Read the released version without accepting arbitrary shell expressions."""
    versions = [
        line.partition("=")[2].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("release_version=")
    ]
    if len(versions) != 1 or not versions[0]:
        raise ValueError("release VERSION must specify one release_version")
    version = versions[0]
    if not all(char.isalnum() or char in ".-_" for char in version):
        raise ValueError("unsafe release_version")
    return version


def _image_exists(name: str) -> bool:
    """Inspect an existing local Docker image; never pull or build."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _has_value(path: Path, key: str) -> bool:
    """Check existence of a secret without exposing or logging its value."""
    if not path.is_file():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = raw.partition("=")
        if separator and name.strip() == key and value.strip():
            return True
    return False


def inspect(root: Path, release: Path = RELEASE) -> dict[str, Any]:
    """Return machine-readable, non-secret prerequisites for live T02."""
    try:
        version = _release_version(release / "VERSION")
    except (FileNotFoundError, ValueError) as exc:
        return {
            "project": "dcems-wtg-control",
            "release_version": "",
            "checks": {"release:version": "MISSING"},
            "missing": ["release:version"],
            "ready_for_start_attempt": False,
            "ready_for_T02_acceptance": False,
            "note": f"release VERSION unavailable: {type(exc).__name__}",
        }
    docker_root = root / "docker"
    config = docker_root / "config"
    # 发布包只导出 Server 与 TimescaleDB；Web 静态页由 Server 镜像内 html/ 提供。
    images = (
        f"dc-ems-server:{version}",
        "dc-ems-timescaledb:2.7.0-pg14",
    )
    checks: dict[str, CHECK] = {}
    for name in images:
        checks[f"image:{name}"] = "READY" if _image_exists(name) else "MISSING"
    required = {
        "seed": root / "data.json",
        "runtime:env": docker_root / "dcems.env",
        "runtime:db": config / "db.property",
        "runtime:jks": config / "ssl" / "dc-ems.jks",
        "runtime:logging": config / "log4j.properties",
    }
    for label, path in required.items():
        checks[label] = "READY" if path.is_file() and path.stat().st_size else "MISSING"
    checks["license:configured"] = (
        "READY" if _has_value(docker_root / "dcems.env", "LICENSE_CODE") else "MISSING"
    )
    checks["database:business_schema"] = "UNVERIFIED"
    checks["server:actual_modbus_fc16"] = "UNVERIFIED"
    missing = sorted(name for name, state in checks.items() if state == "MISSING")
    return {
        "project": "dcems-wtg-control",
        "release_version": version,
        "checks": checks,
        "missing": missing,
        "ready_for_start_attempt": not missing,
        "ready_for_T02_acceptance": False,
        "note": "Schema and actual FC16 require an authorized live Server run.",
    }


def main() -> None:
    """Report missing release resources without dumping credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT / "run" / "dcems-control-wtg",
    )
    parser.add_argument("--release", type=Path, default=RELEASE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = inspect(args.root.resolve(), args.release.resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for name, status in report["checks"].items():
            print(f"{status}: {name}")
        print(f"READY_FOR_START_ATTEMPT={report['ready_for_start_attempt']}")
    if not report["ready_for_start_attempt"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
