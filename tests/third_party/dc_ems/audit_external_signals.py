"""核查 DCEMS AGC、AVC 与一次调频输入的真实工业协议绑定，不产生假阳性。

状态含义：
- BOUND：真实设备类型 + 边缘地址集测点同时存在；
- DERIVED_INTERNAL：由设备 Getter 写入的全场内部量，不是独立协议入口；
- INTERNAL_CONFIG：内部控制使能，不构成工业协议输入；
- UNPROVEN_FACTORY_INPUT：无已证实的设备协议写入路径。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED = ROOT / "third_party/dc-ems/app/server/data.json"
SIGNALS = (
    Path(__file__).resolve().parents[1] / "dc-ems" / "config" / "external_signals_dcems_v1.0.0.yaml"
)


def audit(seed: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """检查不同设备类型的协议测点；不把内部变量视为已经外部接入。"""
    devices = seed.get("devices", [])
    points = seed.get("edgeaddrset", [])
    records: list[dict[str, str]] = []
    for signal in contract["required_signals"]:
        category = str(signal["dcems_device_type"])
        field = signal.get("dcems_field")
        expected = str(signal["industrial_source"])
        if category in {"0", "1", "2"}:
            eligible = [device for device in devices if str(device.get("deviceType")) == category]
            bound = bool(
                field
                and eligible
                and all(
                    any(
                        point.get("setId") == device.get("addrSet")
                        and point.get("varPath") == field
                        and not point.get("isCal")
                        for point in points
                    )
                    for device in eligible
                )
            )
            result = "BOUND" if bound else "MISSING_DEVICE_OR_POINT"
        elif expected == "internal_derived":
            # 设备 Getter 写入的内部量；它不提供独立的工业协议入口，
            # 也不能用同名测点冒充。运行时传播由实时订阅测试单独判定。
            result = "DERIVED_INTERNAL"
        elif expected == "internal_config":
            result = "INTERNAL_CONFIG"
        else:
            # FactoryPoints 并非 WindTurbo/SyzDevice/YctpDevice。
            # 在当前 JAR 中不存在可证明的、到 FactoryPoints 的设备协议写入路径。
            result = "UNPROVEN_FACTORY_INPUT"
        records.append(
            {
                "id": str(signal["id"]),
                "dcems_field": str(field),
                "status": result,
                "expected": expected,
            }
        )
    missing = [
        item["id"]
        for item in records
        if item["status"] not in {"BOUND", "INTERNAL_CONFIG", "DERIVED_INTERNAL"}
    ]
    return {
        "records": records,
        "blocking_signals": missing,
        "all_external_inputs_bound": not missing,
    }


def main() -> None:
    """输出确定性 JSON 审查报告；缺失强制外部输入时退出非零。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--contract", type=Path, default=SIGNALS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-closed", action="store_true")
    args = parser.parse_args()
    seed = json.loads(args.seed.read_text(encoding="utf-8"))
    contract = yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    result = audit(seed, contract)
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if args.require_closed and result["blocking_signals"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
