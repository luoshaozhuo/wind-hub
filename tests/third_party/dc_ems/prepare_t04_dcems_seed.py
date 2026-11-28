"""生成 T04 统一联测的 DCEMS 隔离种子：十机测量 9 点 + 单机控制 2 点。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

import yaml

from tests.third_party.dc_ems.prepare_seed import build_seed, load_mapping
from tests.third_party.dc_ems.validate_gateway_mapping import validate

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "third_party" / "dc-ems" / "app" / "server" / "data.json"
MAPPING = ROOT / "tests" / "third_party" / "dc-ems" / "config" / "mapping_random_smoke.csv"
PROTOCOL_POINTS = ("ctrlActPow", "ctrlReActPow")


def build_t04_seed(
    original: dict[str, Any],
    mapping: dict[str, dict[str, str]],
    host: str,
    port: int,
) -> dict[str, Any]:
    """9 点测量子集 + 2 个已验证控制点；只写隔离输出。"""
    seed = build_seed(original, mapping, host, port, device_count=10, allow_subset=True)
    controls = [
        point for point in original["edgeaddrset"] if point.get("varPath") in PROTOCOL_POINTS
    ]
    if {str(point["varPath"]) for point in controls} != set(PROTOCOL_POINTS):
        raise ValueError("published control points unavailable")
    for point in controls:
        if point.get("isCtrl") is not True or point.get("highBit") != 1:
            raise ValueError("control point contract changed in published seed")
        seed["edgeaddrset"].append(json.loads(json.dumps(point)))
    names = [str(point["varPath"]) for point in seed["edgeaddrset"]]
    if len(names) != len(set(names)):
        raise ValueError("duplicate varPath in derived seed")
    return cast(dict[str, Any], seed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gateway-host", default="host.docker.internal")
    parser.add_argument("--gateway-port", type=int, default=15321)
    parser.add_argument(
        "--point-table",
        type=Path,
        default=ROOT
        / "run"
        / "dcems-t04"
        / "configs"
        / "templates"
        / "point_tables"
        / "point_table_dcems_wtg_unified_v1.0.0.yaml",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if (ROOT / "run").resolve() not in output.parents:
        raise ValueError("T04 seed must be written under wind-hub run/")
    if output.exists():
        raise FileExistsError(output)
    original = json.loads(SOURCE.read_text(encoding="utf-8"))
    mapping = load_mapping(MAPPING)
    # 先按 9 点测量契约校验，再追加控制点（控制契约由 T03 输出契约测试覆盖）。
    measurement = build_seed(
        original,
        mapping,
        args.gateway_host,
        args.gateway_port,
        device_count=10,
        allow_subset=True,
    )
    point_table = yaml.safe_load(args.point_table.read_text(encoding="utf-8"))
    validate(measurement, mapping, point_table)
    seed = build_t04_seed(original, mapping, args.gateway_host, args.gateway_port)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(seed, stream, ensure_ascii=False, indent=2)
    output.chmod(0o600)
    print(output)


if __name__ == "__main__":
    main()
