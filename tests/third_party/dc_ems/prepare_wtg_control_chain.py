"""在已验证的隔离控制根上增配 DCEMS 风机级 Modbus 控制出口。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import yaml

from tests.third_party.dc_ems.prepare_control_chain import prepare as prepare_base

VERSION = "1.2.0"
CONTROL_PORT = 15531
SOURCE_TABLE = "point_table_modbus_wtg_sim_v1.2.0.yaml"
SOURCE_MODEL = "device_model_wtg_sim_physical_v1.2.0.yaml"
MAPPINGS = (
    ("active_power_setpoint", 41557),
    ("reactive_power_setpoint", 41555),
)


def _read(path: Path) -> dict[str, Any]:
    """加载隔离根中的 YAML。"""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _write(path: Path, data: dict[str, Any]) -> None:
    """只更新隔离根中的生成配置。"""
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def prepare(root: Path, *, nats_url: str = "nats://127.0.0.1:14223") -> Path:
    """在单独目录生成风机级控制场景；不改变 PPC 成功基线。"""
    config = prepare_base(root, nats_url=nats_url)
    template_root = config / "templates"
    tables = template_root / "point_tables"
    models = template_root / "device_models"

    table = _read(tables / SOURCE_TABLE)
    table["template_id"] = "modbus_wtg_dcems_control"
    # DCEMS 的 Modbus holding 偏移不减一；字序使用 CDAB。
    # 点值的单位仍遵守 DG/Commander 的 W/var；入口转换位于 Gateway 映射。
    for point_id, address in MAPPINGS:
        point = table["points"][point_id]
        assert point["data_type"] == "float32"
        point["address"]["register_type"] = "holding"
        point["address"]["address"] = address
        point["address"]["count"] = 2
        point["address"]["word_order"] = "little_endian"
    _write(tables / "point_table_modbus_wtg_dcems_control_v1.2.0.yaml", table)

    model = _read(models / SOURCE_MODEL)
    model["template_id"] = "wtg_dcems_control"
    model["point_table"] = {"id": "modbus_wtg_dcems_control", "version": VERSION}
    _write(models / "device_model_wtg_dcems_control_v1.2.0.yaml", model)

    devices_path = config / "instances" / "device_e2e-final_v1.0.1.yaml"
    devices = _read(devices_path)
    devices["devices"]["WTG_SIM01"]["model"] = {
        "id": "wtg_dcems_control",
        "version": VERSION,
    }
    _write(devices_path, devices)

    execution_path = (
        config / "services" / "service_gateway-dcems-control-execution_v1.0.0.yaml"
    )
    execution = _read(execution_path)
    wind = next(
        item for item in execution["servers"] if item["device_id"] == "WTG_SIM01"
    )
    wind["control"] = deepcopy(
        next(
            item["control"]
            for item in execution["servers"]
            if item["device_id"] == "PPC_SIM01"
        )
    )
    wind["control"]["mappings"] = [
        {"variable_id": point_id, "target_point_id": point_id}
        for point_id, _ in MAPPINGS
    ]
    _write(execution_path, execution)

    ingress_path = (
        config / "services" / "service_gateway-dcems-control-ingress_v1.0.0.yaml"
    )
    ingress = _read(ingress_path)
    wind_ingress = deepcopy(wind)
    wind_ingress["server_id"] = "dcems-control-ingress-wtg"
    wind_ingress["listen_port"] = CONTROL_PORT
    wind_ingress["control"].pop("simulation")
    wind_ingress["control"]["mappings"] = [
        {
            "variable_id": point_id,
            "target_point_id": point_id,
            "value_scale": 1000.0,
        }
        for point_id, _ in MAPPINGS
    ]
    ingress["servers"].append(wind_ingress)
    _write(ingress_path, ingress)

    commander_path = config / "services" / "service_commander-dcems-control_v1.0.0.yaml"
    commander = _read(commander_path)
    target = deepcopy(commander["simulation_targets"]["PPC_SIM01"])
    target["gateway_id"] = "gw-wtg-sim01"
    commander["simulation_targets"]["WTG_SIM01"] = target
    _write(commander_path, commander)

    cli_path = config.parent / "state" / "cli.yaml"
    cli = _read(cli_path)
    cli["network_probe"]["industrial_tcp_ports"].append(CONTROL_PORT)
    _write(cli_path, cli)
    _write(config / "services" / "service_cli-dcems-control_v1.0.0.yaml", cli)
    return config


def main() -> None:
    """生成不可覆盖的 DCEMS 风机级独立控制目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:14223")
    args = parser.parse_args()
    print(prepare(args.output, nats_url=args.nats_url))


if __name__ == "__main__":
    main()
