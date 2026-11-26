"""生成隔离的 DCEMS 十风机测试数据库种子；不修改第三方发布包。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any, cast

import yaml

REQUIRED_COLUMNS = frozenset({"dcems_var_path", "business_point_id", "modbus_addr"})
CONTROL_FLAGS = frozenset({"emsActPowerEnable", "emsReactPowerEnable", "bFreCtrlEnableFlag"})


def load_mapping(path: Path) -> dict[str, dict[str, str]]:
    """读取显式的 DCEMS→Gateway BusinessPoint 映射并拒绝重复。"""
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(reader.fieldnames) >= REQUIRED_COLUMNS:
            raise ValueError("mapping needs dcems_var_path,business_point_id,modbus_addr")
        mappings: dict[str, dict[str, str]] = {}
        for row in reader:
            name = row["dcems_var_path"].strip()
            business = row["business_point_id"].strip()
            address = row["modbus_addr"].strip()
            if not name or not business or not address or name in mappings:
                raise ValueError(f"empty or duplicate mapping: {name!r}")
            mappings[name] = {
                "business_point_id": business,
                "modbus_addr": address,
            }
            if row.get("high_bit", "").strip():
                mappings[name]["high_bit"] = row["high_bit"].strip()
            if row.get("coe", "").strip():
                mappings[name]["coe"] = row["coe"].strip()
    return mappings


def build_seed(
    original: dict[str, Any],
    mapping: dict[str, dict[str, str]],
    host: str,
    port: int,
    *,
    device_count: int = 10,
    allow_subset: bool = False,
) -> dict[str, Any]:
    """从历史种子派生单机测试数据，保持原业务变量身份。"""
    if not host or not 1 <= port <= 65535:
        raise ValueError("invalid gateway endpoint")
    if not 1 <= device_count <= 10:
        raise ValueError("device_count must be in 1..10")
    seed = json.loads(json.dumps(original))
    if not seed.get("devices") or not seed.get("edgeServers"):
        raise ValueError("missing DCEMS devices or channels")

    points = seed["edgeaddrset"]
    point_names = {str(point["varPath"]) for point in points}
    if (not allow_subset and point_names != set(mapping)) or not set(mapping) <= point_names:
        missing = sorted(point_names - set(mapping))
        extra = sorted(set(mapping) - point_names)
        raise ValueError(f"business mapping not closed: missing={missing}, extra={extra}")
    if allow_subset:
        points = [point for point in points if str(point["varPath"]) in mapping]
        seed["edgeaddrset"] = points

    # DCEMS 历史点表将部分读写变量映射到同一物理地址。
    # Gateway 的 RegisterMap 禁止地址重叠，必须将这些别名折叠为
    # 单个业务点 ID；不同地址不得共用一个业务点 ID。
    address_to_business: dict[str, str] = {}
    business_to_address: dict[str, str] = {}
    for name, row in mapping.items():
        address = row["modbus_addr"]
        business = row["business_point_id"]
        if address in address_to_business and address_to_business[address] != business:
            raise ValueError(f"aliases disagree at {address}: {name}")
        if business in business_to_address and business_to_address[business] != address:
            raise ValueError(f"business point reused across addresses: {business}")
        address_to_business[address] = business
        business_to_address[business] = address

    for point in points:
        name = str(point["varPath"])
        actual = mapping[name]["modbus_addr"]
        if not allow_subset and actual != point["addr"]:
            raise ValueError(f"address mismatch for {name}: {actual} != {point['addr']}")
        segments = actual.split(".")
        if len(segments) != 3 or not all(value.isdecimal() for value in segments):
            raise ValueError(f"invalid Modbus address: {actual}")
        point["addr"] = actual
        if allow_subset and "high_bit" in mapping[name]:
            point["highBit"] = int(mapping[name]["high_bit"])
        if allow_subset and "coe" in mapping[name]:
            point["coe"] = float(mapping[name]["coe"])

    if len(seed["devices"]) < device_count or len(seed["edgeServers"]) < device_count:
        raise ValueError("historical seed has insufficient devices or channels")
    devices = []
    servers = []
    for index in range(device_count):
        device = seed["devices"][index]
        device["deviceId"] = f"WTG{index + 1:02d}"
        device["deviceName"] = f"DCEMS WTG {index + 1:02d}"
        device["serverId"] = index + 1
        device["addrSet"] = points[0]["setId"]
        device["lineNo"] = 1
        devices.append(device)

        server = seed["edgeServers"][index]
        server.update(
            serverName=f"wind-hub-gateway-{index + 1:02d}",
            serverIp1=host,
            port1=port + index,
            serverIp2=None,
            port2=None,
            serverType="MODBUSTCP",
            interval=500,
        )
        if port + index > 65535:
            raise ValueError("Gateway endpoint range exceeds TCP port limit")
        servers.append(server)
    seed["devices"] = devices
    seed["edgeServers"] = servers
    seed["syncdata"] = []
    seed["sysconfig"] = [
        row
        for row in seed["sysconfig"]
        if not (row["groupName"] in {"sync", "watch"} and "redis" in str(row["itemName"]).lower())
    ]
    license_code = os.environ.get("DCEMS_LICENSE_CODE", "")
    for row in seed["emsConfig"]:
        if row["varPath"] == "iWTUnits":
            row["val"] = str(device_count)
        if row["varPath"] == "iLineCount":
            row["val"] = "1"
        if row["varPath"] in {
            "emsActPowerEnable",
            "emsReactPowerEnable",
            "bFreCtrlEnableFlag",
            "kk_flag",
        }:
            row["val"] = "false"
        if row["varPath"] == "license":
            row["val"] = license_code
    flags = [row for row in seed["emsConfig"] if row["varPath"] in CONTROL_FLAGS]
    if len(flags) != len(CONTROL_FLAGS) or {row["varPath"] for row in flags} != CONTROL_FLAGS:
        raise ValueError("smoke seed must contain exactly one of each control enable flag")
    if any(row["val"] != "false" for row in flags):
        raise ValueError("automatic control must be disabled in smoke seed")
    return cast(dict[str, Any], seed)


def main() -> None:
    """仅输出到调用方指定的隔离路径，绝不改写发布包内 data.json。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gateway-host", required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--device-count", type=int, default=10)
    parser.add_argument("--smoke-subset", action="store_true")
    parser.add_argument("--with-auxiliary", action="store_true")
    parser.add_argument("--gateway-point-table", type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.seed.read_text(encoding="utf-8"))
    mapping = load_mapping(args.mapping)
    derived = build_seed(
        original,
        mapping,
        args.gateway_host,
        args.gateway_port,
        device_count=args.device_count,
        allow_subset=args.smoke_subset,
    )
    from tests.third_party.dc_ems.validate_gateway_mapping import validate

    point_table = yaml.safe_load(args.gateway_point_table.read_text(encoding="utf-8"))
    validate(derived, mapping, point_table)
    if args.with_auxiliary:
        from tests.third_party.dc_ems.auxiliary_seed import append_auxiliary_devices

        required = {
            "sim_plant_active_power",
            "sim_plant_reactive_power",
            "primary_frequency_mode",
        }
        if not required <= set(point_table["points"]):
            raise ValueError("Gateway PointTable lacks station or PFR source points")
        derived = append_auxiliary_devices(derived)
        validate(derived, mapping, point_table)
    if args.output.resolve() == args.seed.resolve():
        raise ValueError("refuse to overwrite packaged data.json")
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(derived, stream, ensure_ascii=False, indent=2)
    args.output.chmod(0o600)


if __name__ == "__main__":
    main()
