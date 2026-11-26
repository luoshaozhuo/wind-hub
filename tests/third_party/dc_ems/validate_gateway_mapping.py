"""验证 DCEMS varPath、业务点名及 Gateway Modbus 点表的联合契约。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from tests.third_party.dc_ems.prepare_seed import load_mapping


AREAS = {"0": "coil", "1": "discrete_input", "3": "input", "4": "holding"}
TYPES = {"F": "float32", "I": "int32", "B": "bool"}
AUXILIARY = {
    "powAct": ("sim_plant_active_power", "1.3.2", 0.001, "watt"),
    "powReact": ("sim_plant_reactive_power", "1.3.4", 0.001, "var"),
    "isFreMode": ("primary_frequency_mode", "1.1.0", None, "none"),
}


def validate(
    seed: dict[str, Any],
    mapping: dict[str, dict[str, str]],
    point_table: dict[str, Any],
) -> None:
    """拒绝名称、协议地址、字序、类型或读写权限不一致的配置。"""
    points = point_table.get("points")
    if not isinstance(points, dict):
        raise ValueError("Gateway point table must contain points mapping")
    aliases: dict[str, str] = {}
    names = {str(row["varPath"]) for row in seed["edgeaddrset"]}
    auxiliary = names - set(mapping)
    if auxiliary and auxiliary != set(AUXILIARY):
        raise ValueError(f"DCEMS mapping not closed: {sorted(names ^ set(mapping))}")
    if not set(mapping) <= names:
        raise ValueError(f"DCEMS mapping not closed: {sorted(names ^ set(mapping))}")
    complete = dict(mapping)
    for name in auxiliary:
        business, address, _, _ = AUXILIARY[name]
        complete[name] = {"business_point_id": business, "modbus_addr": address}
    for row in seed["edgeaddrset"]:
        name = str(row["varPath"])
        target = complete[name]
        if name in auxiliary:
            business, expected_address, scale, unit = AUXILIARY[name]
            if row.get("addr") != expected_address or row.get("coe") != scale:
                raise ValueError(f"{name}: invalid auxiliary DCEMS address or conversion")
            if points[business].get("unit") != unit:
                raise ValueError(f"{name}: Gateway physical unit mismatch")
        business = target["business_point_id"]
        if business not in points:
            raise ValueError(f"{name}: Gateway business point missing: {business}")
        spec = points[business]
        if spec.get("business_point_id") != business:
            raise ValueError(f"{name}: point key is not its BusinessPoint ID: {business}")
        chunks = target["modbus_addr"].split(".")
        if len(chunks) != 3 or chunks[1] not in AREAS:
            raise ValueError(f"{name}: unsupported DCEMS Modbus address")
        address = spec.get("address", {})
        area = AREAS[chunks[1]]
        if (
            address.get("register_type") != area
            or address.get("address") != int(chunks[2])
            or address.get("slave_id", 1) != int(chunks[0])
        ):
            raise ValueError(f"{name}: Gateway address mismatch")
        if spec.get("data_type") != TYPES.get(row["dataType"]):
            raise ValueError(f"{name}: logical type mismatch")
        if row["dataType"] in {"F", "I"}:
            order = "little_endian" if row.get("highBit") == 1 else "big_endian"
            if address.get("word_order", "big_endian") != order:
                raise ValueError(f"{name}: word order mismatch")
        if row.get("isCtrl") is True and area not in {"coil", "holding"}:
            raise ValueError(f"{name}: control is mapped to read-only register")
        previous = aliases.setdefault(target["modbus_addr"], business)
        if previous != business:
            raise ValueError(f"{name}: one address has inconsistent business aliases")


def main() -> None:
    """以真实发布点表为依据执行外部协议配置预检。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--point-table", type=Path, required=True)
    args = parser.parse_args()
    seed = json.loads(args.seed.read_text(encoding="utf-8"))
    point_table = yaml.safe_load(args.point_table.read_text(encoding="utf-8"))
    validate(seed, load_mapping(args.mapping), point_table)
    print("DCEMS Gateway BusinessPoint and Modbus contract: OK")


if __name__ == "__main__":
    main()
