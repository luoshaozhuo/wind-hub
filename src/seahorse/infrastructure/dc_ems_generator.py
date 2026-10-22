"""基于 dc-ems 原始 49 点和 50 台风机生成隔离虚拟部署配置。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from seahorse.infrastructure.dc_ems_contract import (
    OriginalAddress,
    RegisterArea,
    validate_original_addresses,
)


def generate_profile(
    source_path: Path,
    output_directory: Path,
    *,
    start_port: int = 15120,
    enable_control: bool = False,
    command_bind: str = "127.0.0.1:50072",
) -> None:
    source: dict[str, Any] = json.loads(source_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = source["points"]
    devices: list[dict[str, Any]] = source["devices"]
    if len(entries) != 49 or len(devices) != 50:
        raise ValueError("expected exactly 49 points and 50 devices")
    if not 1024 <= start_port <= 65535 - len(devices) + 1:
        raise ValueError("virtual port range invalid")
    if not command_bind.startswith("127.0.0.1:"):
        raise ValueError("virtual Commander must bind loopback")
    addresses: dict[str, OriginalAddress] = {}
    for point in entries:
        slave, register, offset = map(int, point["address"].split("."))
        areas: dict[int, RegisterArea] = {
            0: "coil",
            1: "discrete_input",
            3: "input",
            4: "holding",
        }
        area = areas.get(register)
        if area is None or point["high_bit"] != 1 or slave != 1:
            raise ValueError(f"unsupported DC-EMS address/encoding: {point['name']}")
        addresses[point["name"]] = OriginalAddress(slave, area, offset, point["kind"])
    validate_original_addresses(addresses)

    # 同址读写只对应一个物理寄存器；以真实控制点为规范名。
    canonical: dict[tuple[int, str, int], dict[str, Any]] = {}
    for point in entries:
        addr = addresses[point["name"]]
        key = (addr.slave, addr.area, addr.offset)
        old = canonical.get(key)
        if old is None or (point["command"] and not old["command"]):
            canonical[key] = point

    points: dict[str, Any] = {}
    for point in canonical.values():
        addr = addresses[point["name"]]
        role = "command" if point["command"] else "feedback"
        points[point["name"]] = {
            "role": role,
            "data_type": {"F": "float32", "I": "int32", "B": "bool"}[addr.kind],
            "address": {
                "register_type": addr.area,
                "address": addr.offset,
                "word_order": "little_endian",
            },
            **({"max_age_seconds": 3.0} if role == "feedback" else {}),
        }
    if len(points) != 45:
        raise ValueError("49 source entries must resolve to 45 unique registers")

    endpoints: dict[str, Any] = {}
    gateway_devices: dict[str, Any] = {}
    overrides: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        device_id = device["id"]
        endpoint_id = f"turbine_{index + 1:03d}"
        port = start_port + index
        endpoints[endpoint_id] = {
            "protocol": "modbus", "host": "127.0.0.1", "port": port
        }
        gateway_devices[device_id] = {
            "endpoint_id": endpoint_id,
            "point_table_id": "dc_ems_turbine",
            "protocol_address": {"unit_id": 1},
            "commands": {
                name: {"device_id": device_id, "point_id": name}
                for name, point in points.items() if point["role"] == "command"
            },
        }
        overrides.append({
            "server_id": device["server_id"],
            "device_id": device_id,
            "original_host": device["ip"],
            "original_port": device["port"],
            "virtual_host": "127.0.0.1",
            "virtual_port": port,
        })
    if len(set(gateway_devices)) != 50:
        raise ValueError("duplicate turbine device ID")

    gateway = {
        "data_grpc_bind": "127.0.0.1:50073",
        "endpoints": endpoints,
        "point_tables": {"dc_ems_turbine": {"protocol": "modbus", "points": points}},
        "devices": gateway_devices,
        "control": {
            "enabled": enable_control,
            "commander": {
                "endpoint": command_bind, "command_timeout": 3.0, "rpc_timeout": 4.0
            },
            "journal_path": str(output_directory / "commands.json"),
            "access": {
                "data_writers": [],
                "command_writers": ["127.0.0.1"] if enable_control else [],
            },
        },
    }
    seahorse = {
        "step_seconds": 0.5,
        "seed": 42,
        "wind_field": {"model": "simple"},
        "grid": {"model": "simple"},
        "substation": {"model": "simple"},
        "turbines": [
            {
                "device_id": device["id"],
                "model": "simple",
                "rated_power_kw": device["capacity_kw"],
            }
            for device in devices
        ],
        "dispatch_station": {"model": "simple"},
        "primary_station": {"model": "simple"},
        "inertia_station": {"model": "simple"},
        "dispatch_points": {},
        "primary_points": {},
        "inertia_points": {},
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, config in (("gateway.yaml", gateway), ("seahorse.yaml", seahorse)):
        (output_directory / name).write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    (output_directory / "dc_ems_endpoint_overrides.json").write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="seahorse-dc-ems-profile")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-port", type=int, default=15120)
    parser.add_argument("--enable-virtual-control", action="store_true")
    args = parser.parse_args()
    generate_profile(
        args.source,
        args.output,
        start_port=args.start_port,
        enable_control=args.enable_virtual_control,
    )


if __name__ == "__main__":
    main()
