"""生成隔离的十机完整测量链路配置，不修改原有 Smoke 配置。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import yaml

from tests.third_party.dc_ems.stage_config import ROOT, stage

DEPLOYMENT = "dcems-measurement"
VERSION = "1.0.0"
SOURCE_PORT = 15221
TARGET_PORT = 15121
STATION_POINTS = frozenset(
    {"sim_plant_active_power", "sim_plant_reactive_power", "primary_frequency_mode"}
)


def _read(path: Path) -> dict[str, Any]:
    """读取 YAML 配置。"""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _write(path: Path, document: dict[str, Any]) -> None:
    """写入受控测试根下的 YAML 配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def prepare(
    root: Path,
    *,
    nats_url: str = "nats://127.0.0.1:14222",
    read_merge_max_gap: int = 0,
    source_port_base: int = SOURCE_PORT,
) -> Path:
    """创建独立配置树；所有输入来自仓库中已发布的隔离样板。"""
    root = root.resolve()
    if root.exists():
        raise FileExistsError(root)
    if root == ROOT or ROOT in root.parents and root.parts[-1] != "dcems-measurement":
        raise ValueError("full-chain root must be a dedicated dcems-measurement directory")
    config = root / "configs"
    stage(config)

    template_dir = config / "templates"
    # Collector 要求模板根中 business_points 只有一个选中版本。
    # e2e 样板带入的 1.5.0 与本次点表引用的 1.6.0 冲突，属于废弃副本。
    for obsolete_business_points in template_dir.glob("business_points_standard_v*.yaml"):
        if obsolete_business_points.name != "business_points_standard_v1.6.0.yaml":
            obsolete_business_points.unlink()
    table_path = template_dir / "point_tables" / "point_table_dcems_wtg_random_v1.0.0.yaml"
    full_table = _read(table_path)
    compact_table = deepcopy(full_table)
    compact_table["template_id"] = "dcems_wtg_measurement"
    compact_table["points"] = {
        name: point for name, point in full_table["points"].items() if name not in STATION_POINTS
    }
    _write(
        template_dir / "point_tables" / "point_table_dcems_wtg_measurement_v1.0.0.yaml",
        compact_table,
    )
    model = _read(template_dir / "device_models" / "device_model_dcems_wtg_random_v1.0.0.yaml")
    model["template_id"] = "dcems_wtg_measurement"
    model["point_table"] = {"id": "dcems_wtg_measurement", "version": VERSION}
    _write(
        template_dir / "device_models" / "device_model_dcems_wtg_measurement_v1.0.0.yaml",
        model,
    )

    instance_dir = config / "instances"
    original_devices = _read(instance_dir / "device_dcems-smoke_v1.0.0.yaml")
    source_devices = deepcopy(original_devices)
    for index in range(2, 11):
        source_devices["devices"][f"WTG{index:02d}"]["model"] = {
            "id": "dcems_wtg_measurement",
            "version": VERSION,
        }
    for index, device in enumerate(source_devices["devices"].values()):
        device["group"] = "dcems-source"
        device["endpoint"]["port"] = source_port_base + index
        # 精简点表在机侧存在地址空洞；Collector 默认 max_gap=8 会合并
        # 跨越未定义寄存器的读取，Gateway 按协议返回 IllegalDataAddress。
        # 关闭跨空洞合并，只按点表实际地址发起读取。
        device["overrides"] = {
            **device.get("overrides", {}),
            "protocol_options": {"read_merge_max_gap": read_merge_max_gap},
        }
    _write(instance_dir / "device_dcems-source_v1.0.0.yaml", source_devices)

    target_devices = deepcopy(source_devices)
    for index in range(1, 11):
        device = target_devices["devices"][f"WTG{index:02d}"]
        device["group"] = "dcems-target"
        device["endpoint"]["port"] = TARGET_PORT + index - 1
    _write(instance_dir / "device_dcems-target_v1.0.0.yaml", target_devices)
    _write(
        instance_dir / "collector_tasks_dcems-source_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "tasks": {
                "dcems_source_all": {
                    "group": "dcems-source",
                    "point_group": "all",
                    "interval_ms": 500,
                    "desired_state": "START",
                }
            },
        },
    )

    source_gateway = _read(config / "services" / "service_gateway-dcems-smoke_v1.0.0.yaml")
    source_gateway["service_id"] = "gateway-dcems-source"
    source_gateway["config_sources"]["instances"]["devices"] = "dcems-source"
    _write(config / "services" / "service_gateway-dcems-source_v1.0.0.yaml", source_gateway)

    target_gateway = deepcopy(source_gateway)
    target_gateway["service_id"] = "gateway-dcems-target"
    target_gateway["config_sources"]["instances"]["devices"] = "dcems-target"
    target_gateway["grpc"]["listen_port"] = 52074
    target_gateway["grpc"]["sink_listener_port"] = 52075
    target_gateway["security"]["callers"] = [
        {
            "caller_id": "operator",
            "token_env": "WIND_HUB_DCE_TARGET_OPERATOR_TOKEN",
            "permissions": ["read", "lifecycle", "diagnose"],
        },
        {
            "caller_id": "sink",
            "token_env": "WIND_HUB_DCE_TARGET_SINK_TOKEN",
            "permissions": ["inject"],
        },
    ]
    for index, server in enumerate(target_gateway["servers"]):
        server["server_id"] = f"dcems-target-{index + 1:02d}"
        server["listen_port"] = TARGET_PORT + index
    _write(config / "services" / "service_gateway-dcems-target_v1.0.0.yaml", target_gateway)

    generator = _read(config / "services" / "service_data-generator-dcems-smoke_v1.0.0.yaml")
    generator["config_sources"]["instances"]["devices"] = "dcems-source"
    _write(
        config / "services" / "service_data-generator-dcems-measurement_v1.0.0.yaml",
        {**generator, "service_id": "data-generator-dcems-measurement"},
    )

    collector = _read(
        ROOT
        / "deploy"
        / "config_roots"
        / "e2e-final"
        / "services"
        / "service_collector-e2e-realtime_v2.0.0.yaml"
    )
    collector["service_id"] = "collector-dcems-realtime"
    collector["config_sources"]["instances"] = {
        "site": "dcems-smoke",
        "devices": "dcems-source",
        "tasks": "dcems-source",
    }
    collector["messaging"]["servers"] = [nats_url]
    collector["messaging"]["max_staleness_ms"] = 3000
    _write(config / "services" / "service_collector-dcems-realtime_v2.0.0.yaml", collector)

    sink = _read(
        ROOT / "deploy" / "config_roots" / "e2e-final" / "services" / "service_sink-e2e_v2.0.0.yaml"
    )
    sink["service_id"] = "sink-dcems-measurement"
    sink["deployment_id"] = DEPLOYMENT
    sink["wind_farm_id"] = "dcems-smoke"
    sink["config_root"] = str(root)
    # 专项不运行 wind-hub Web，Sink 只开放 CLI 最小授权。
    sink["security"]["callers"] = [
        {
            "caller_id": "cli",
            "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
            "permissions": ["query", "control", "config"],
        }
    ]
    sink["config_sources"]["instances"] = {
        "sinks": "dcems-measurement",
        "deliveries": "dcems-measurement",
    }
    sink["nats"]["servers"] = [nats_url]
    sink["grpc"]["listen_port"] = 52053
    _write(config / "services" / "service_sink-dcems-measurement_v2.0.0.yaml", sink)

    sinks: dict[str, Any] = {}
    deliveries: dict[str, Any] = {}
    for index in range(1, 11):
        device_id = f"WTG{index:02d}"
        sink_id = f"dcems_gateway_{index:02d}"
        points = list(full_table["points"] if index == 1 else compact_table["points"])
        sinks[sink_id] = {
            "template_id": "gateway_realtime",
            "template_version": "1.1.0",
            "connection": {
                "endpoint": "127.0.0.1:52075",
                "gateway_id": f"dcems-target-{index:02d}",
            },
            "overrides": {},
        }
        deliveries[f"dcems_{device_id.lower()}"] = {
            "template_id": "realtime_power",
            "template_version": "1.1.0",
            "sink_id": sink_id,
            "desired_state": "START",
            "source": {
                "wind_farm_id": "dcems-smoke",
                "collector_id": "collector-dcems-realtime",
                "devices": [device_id],
                "points": points,
            },
            "target_mappings": [
                {
                    "source_device_id": device_id,
                    "source_point_id": point_id,
                    "variable_id": point_id,
                }
                for point_id in points
            ],
            "overrides": {},
        }
    _write(
        instance_dir / "sinks_dcems-measurement_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "revision": "dcems-measurement-sinks-r1",
            "sinks": sinks,
        },
    )
    _write(
        instance_dir / "deliveries_dcems-measurement_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "revision": "dcems-measurement-deliveries-r1",
            "deliveries": deliveries,
        },
    )

    _write(
        config / "deployments" / "deployment_dcems-measurement_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "deployment_id": DEPLOYMENT,
            "services": {
                name: {"config_id": name, "file_version": version}
                for name, version in (
                    ("gateway-dcems-source", VERSION),
                    ("gateway-dcems-target", VERSION),
                    ("data-generator-dcems-measurement", VERSION),
                    ("collector-dcems-realtime", "2.0.0"),
                    ("sink-dcems-measurement", "2.0.0"),
                )
            },
            "instances": {
                "site": {"dcems-smoke": {"file_version": VERSION}},
                "device": {
                    "dcems-source": {"file_version": VERSION},
                    "dcems-target": {"file_version": VERSION},
                },
                "scenario": {"dcems-smoke": {"file_version": VERSION}},
                "collector_tasks": {"dcems-source": {"file_version": VERSION}},
                "sink": {"dcems-measurement": {"file_version": VERSION}},
                "delivery": {"dcems-measurement": {"file_version": VERSION}},
            },
        },
    )
    # 删除仅供原单 Gateway Smoke 使用的派生副本，避免误选旧 Deployment。
    for obsolete in (
        "services/service_gateway-dcems-smoke_v1.0.0.yaml",
        "services/service_data-generator-dcems-smoke_v1.0.0.yaml",
        "deployments/deployment_dcems-smoke_v1.0.0.yaml",
        "instances/device_dcems-smoke_v1.0.0.yaml",
    ):
        (config / obsolete).unlink()
    return config


def main() -> None:
    """建立不复用既有输出目录的联测配置。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:14222")
    parser.add_argument("--read-merge-max-gap", type=int, default=0)
    parser.add_argument("--source-port-base", type=int, default=SOURCE_PORT)
    args = parser.parse_args()
    print(
        prepare(
            args.output,
            nats_url=args.nats_url,
            read_merge_max_gap=args.read_merge_max_gap,
            source_port_base=args.source_port_base,
        )
    )


if __name__ == "__main__":
    main()
