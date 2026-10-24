"""基于 dc-ems 原始 49 点和 50 台风机生成隔离虚拟部署配置。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from industrial_gateway.infrastructure.config.loader import GatewayConfig
from industrial_gateway.infrastructure.protocol.modbus.codec import (
    RegisterMapping,
    encode_registers,
)
from seahorse.infrastructure.config import SeahorseConfig
from seahorse.infrastructure.dc_ems_contract import (
    OriginalAddress,
    RegisterArea,
    validate_original_addresses,
)
from seahorse.infrastructure.factory import create_runtime


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
        raise ValueError("expected exactly 49 turbine points and 50 turbine devices")
    external_devices: list[dict[str, Any]] = source.get("external_devices", [])
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
        endpoints[endpoint_id] = {"protocol": "modbus", "host": "127.0.0.1", "port": port}
        gateway_devices[device_id] = {
            "endpoint_id": endpoint_id,
            "point_table_id": "dc_ems_turbine",
            "protocol_address": {"unit_id": 1},
            "commands": {
                name: {"device_id": device_id, "point_id": name}
                for name, point in points.items()
                if point["role"] == "command"
            },
        }
        overrides.append(
            {
                "server_id": device["server_id"],
                "device_id": device_id,
                "original_host": device["ip"],
                "original_port": device["port"],
                "virtual_host": "127.0.0.1",
                "virtual_port": port,
            }
        )
    if len(set(gateway_devices)) != 50:
        raise ValueError("duplicate turbine device ID")

    # 非风机设备必须使用来自 DC-EMS 的实际设备、地址与测点清单。
    # 没有依据时不创建虚构寄存器；缺失字段在启动前显式失败。
    external_tables: dict[str, Any] = {}
    for external in external_devices:
        device_id = external["id"]
        if device_id in gateway_devices:
            raise ValueError(f"duplicate external device ID: {device_id}")
        model_id = external["model_id"]
        if model_id not in {"substation", "dispatch", "primary", "inertia"}:
            raise ValueError(f"unsupported simulated device: {model_id}")
        if device_id != model_id or model_id in gateway_devices:
            raise ValueError(f"external device must use unique model ID: {model_id}")
        point_items = external["points"]
        if not point_items:
            raise ValueError(f"external device lacks point mapping: {device_id}")
        table_id = f"external_{device_id}"
        external_points: dict[str, Any] = {}
        for item in point_items:
            name = item["name"]
            if name in external_points:
                raise ValueError(f"duplicate external point: {device_id}.{name}")
            if item["role"] != "feedback":
                raise ValueError("non-turbine control mappings are not implemented")
            external_points[name] = {
                "role": "feedback",
                "data_type": item["data_type"],
                "address": {
                    "register_type": item["register_type"],
                    "address": item["address"],
                    "word_order": item["word_order"],
                },
                "max_age_seconds": 3.0,
            }
        external_tables[table_id] = {"protocol": "modbus", "points": external_points}
        endpoint_id = f"external_{device_id}"
        if endpoint_id in endpoints:
            raise ValueError(f"duplicate endpoint: {endpoint_id}")
        endpoints[endpoint_id] = {
            "protocol": "modbus",
            "host": "127.0.0.1",
            "port": external["port"],
        }
        gateway_devices[model_id] = {
            "endpoint_id": endpoint_id,
            "point_table_id": table_id,
            "protocol_address": {"unit_id": external["unit_id"]},
            "commands": {},
        }
        overrides.append(
            {
                "server_id": external["server_id"],
                "device_id": device_id,
                "original_host": external["ip"],
                "original_port": external["original_port"],
                "virtual_host": "127.0.0.1",
                "virtual_port": external["port"],
            }
        )

    gateway = {
        "data_grpc_bind": "127.0.0.1:50073",
        "endpoints": endpoints,
        "point_tables": {
            "dc_ems_turbine": {"protocol": "modbus", "points": points},
            **external_tables,
        },
        "devices": gateway_devices,
        "control": {
            "enabled": enable_control,
            "commander": {"endpoint": command_bind, "command_timeout": 3.0, "rpc_timeout": 4.0},
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
    gateway_config = GatewayConfig.model_validate(gateway)
    model_config = SeahorseConfig.model_validate(seahorse)
    snapshot = create_runtime(model_config).step()
    samples = {device.device_id: device.points for device in snapshot.devices}
    for device_id, gateway_device in gateway_config.devices.items():
        actual = samples.get(device_id)
        if actual is None:
            raise ValueError(f"missing simulated device: {device_id}")
        table = gateway_config.point_tables[gateway_device.point_table_id]
        for name, gateway_point in table.points.items():
            if gateway_point.role != "feedback":
                continue
            if name not in actual:
                raise ValueError(f"missing simulated feedback: {device_id}.{name}")
            address = gateway_point.address
            mapping = RegisterMapping(
                device_id,
                name,
                address.register_type,
                address.address,
                gateway_point.data_type,
                address.word_order,
            )
            try:
                encode_registers(mapping, actual[name])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid simulated feedback: {device_id}.{name}") from exc
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
