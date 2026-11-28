"""生成 T04 十机测量 + 单机控制统一联测配置根。

在同一测试根内组合：
- 十机测量：DG(random×9 + physical×1) → Gateway2(source) → Collector → Sink
  → Gateway1(target) → DCEMS 原始 Server；
- 单机控制：DCEMS → FC16 → Gateway1(target WTG01) → Commander →
  Gateway2(source WTG01 执行) → DG 物理风机；
- 测量与控制共享同一设备身份（WTG01..WTG10）与同一批 Gateway 实例。

只写入新的隔离运行根；不修改 third_party/dc-ems、deploy 生产模板与既有测试根。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import yaml

from tests.third_party.dc_ems.stage_config import ROOT, stage

VERSION = "1.0.0"
DEPLOYMENT = "dcems-t04"
SOURCE_PORT = 15221
TARGET_PORT = 15121
COMMANDER_GRPC = 52152
CONTROL_MAP: dict[str, tuple[str, float]] = {
    # DCEMS 控制点 → (wind-hub 变量, kW/kvar→W/var 换算)
    "ctrlActPow": ("active_power_setpoint", 1000.0),
    "ctrlReActPow": ("reactive_power_setpoint", 1000.0),
}
# 物理 WTG01 场景点（全部为物理引擎输出），源点 → 目标变量。
WTG01_SOURCE_POINTS = (
    "wind_speed",
    "active_power",
    "reactive_power",
    "running_status",
    "fault_status",
    "power_limit_setpoint",
    "sim_generator_speed",
    "pitch_angle_1",
    "control_mode",
    "active_power_setpoint",
    "reactive_power_setpoint",
)
WTG01_DELIVERY_POINTS = tuple(
    point
    for point in WTG01_SOURCE_POINTS
    if point
    not in {
        "control_mode",
        "active_power_setpoint",
        "reactive_power_setpoint",
    }
)
WTG01_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("wind_speed", "wind_speed"),
    ("active_power", "sim_active_power"),
    ("reactive_power", "sim_reactive_power"),
    ("running_status", "sim_running_status"),
    ("fault_status", "device_fault_active"),
    ("power_limit_setpoint", "active_power_limit"),
    ("sim_generator_speed", "generator_speed"),
    ("pitch_angle_1", "blade_1_pitch_angle"),
    ("pitch_angle_1", "pitch_angle"),
)
RANDOM_POINTS = (
    "wind_speed",
    "sim_running_status",
    "sim_active_power",
    "sim_reactive_power",
    "device_fault_active",
    "active_power_limit",
    "generator_speed",
    "blade_1_pitch_angle",
    "pitch_angle",
)
# 每台随机风机的恒定风速（身份刺激）与其余点的确定性取值。
RANDOM_CONSTANTS: dict[str, dict[str, float | int | bool]] = {
    f"WTG{index:02d}": {
        "wind_speed": 6.0 + index,
        "sim_running_status": 1,
        "sim_active_power": 200000.0 + index * 50000.0,
        "sim_reactive_power": -50000.0 + index * 10000.0,
        "device_fault_active": False,
        "active_power_limit": 2000000.0,
        "generator_speed": 140.0 + index,
        "blade_1_pitch_angle": 4.0 + index * 0.1,
        "pitch_angle": 3.0 + index * 0.1,
    }
    for index in range(2, 11)
}


def _read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _random_models() -> dict[str, Any]:
    models: dict[str, Any] = {}
    for index in range(2, 11):
        device = f"WTG{index:02d}"
        values = RANDOM_CONSTANTS[device]
        models[f"rand-{index:02d}"] = {
            "kind": "random",
            "seed": 100 + index,
            "rules": {
                point: {"distribution": "constant", "value": values[point]}
                for point in RANDOM_POINTS
            },
        }
    return models


def _scenario() -> dict[str, Any]:
    # 物理设备不得在场景内声明 points（属于设备模板职责）；
    # 场景点默认取物理点表全部点，物理引擎必须逐点提供输出。
    devices: list[dict[str, Any]] = [
        {
            "device_id": "WTG01",
            "weather_ref": "site-wtg01",
            "controller_mode": "local",
        }
    ]
    for index in range(2, 11):
        device = f"WTG{index:02d}"
        devices.append(
            {"device_id": device, "model_ref": f"rand-{index:02d}", "points": list(RANDOM_POINTS)}
        )
    physical_points = tuple(
        point
        for point in (
            _read(
                ROOT
                / "deploy"
                / "config_roots"
                / "e2e-final"
                / "templates"
                / "point_tables"
                / "point_table_modbus_wtg_sim_v1.2.0.yaml"
            )["points"]
        )
    )
    gateways: list[dict[str, Any]] = [
        {
            "gateway_id": "dcems-source-01",
            "endpoint": "127.0.0.1:52073",
            "device_id": "WTG01",
            "variable_mapping": {point: point for point in physical_points},
        }
    ]
    for index in range(2, 11):
        device = f"WTG{index:02d}"
        gateways.append(
            {
                "gateway_id": f"dcems-source-{index:02d}",
                "endpoint": "127.0.0.1:52073",
                "device_id": device,
                "variable_mapping": {point: point for point in RANDOM_POINTS},
            }
        )
    return {
        "schema_version": 1,
        "file_version": VERSION,
        "scenarios": [
            {
                "scenario_id": "t04-unified-ten",
                "version": VERSION,
                "description": "T04 unified ten-WTG measurement with physical WTG01 control",
                "source": {"kind": "simulation", "interval_seconds": 0.05, "seed": 42},
                "weather": {
                    "site-wtg01": {
                        "wind": {
                            "wind_seed": 42,
                            "wind_correlation_time_s": 8.0,
                            "wind_speed_mean": 9.5,
                            "turbulence_intensity": 0.12,
                            "wind_direction": 0.0,
                            "air_density": 1.225,
                        },
                        "irradiance": {
                            "sunrise_s": 21600,
                            "sunset_s": 64800,
                            "irradiance_clear_w_m2": 1000.0,
                            "cloud_mode": "constant",
                            "cloud_factor_target": 0.8,
                        },
                        "ambient_temperature": {
                            "temperature_mean_c": 20.0,
                            "diurnal_amplitude_k": 5.0,
                            "temperature_seed": 43,
                            "temperature_tau_s": 1800.0,
                            "temperature_sigma_k": 0.0,
                            "phase_rad": 0.0,
                        },
                    }
                },
                "models": _random_models(),
                "devices": devices,
                "gateways": gateways,
            }
        ],
    }


def _unified_point_table(
    full_random: dict[str, Any], control_points: dict[str, Any]
) -> dict[str, Any]:
    points = dict(full_random["points"])
    # DCEMS 控制寄存器沿用 T03 已验证地址与 CDAB 字序（holding 41557/41555）。
    for name, address in (("active_power_setpoint", 41557), ("reactive_power_setpoint", 41555)):
        point = deepcopy(control_points[name])
        point["point_groups"] = ["control"]
        point["address"] = {
            "register_type": "holding",
            "address": address,
            "count": 2,
            "slave_id": 1,
            "word_order": "little_endian",
        }
        points[name] = point
    return {
        "schema_version": full_random.get("schema_version", 1),
        "file_version": VERSION,
        "template_id": "dcems_wtg_unified",
        "config_sources": full_random.get("config_sources", []),
        "protocol": full_random.get("protocol", "modbus"),
        "points": points,
    }


def prepare(
    root: Path,
    *,
    nats_url: str = "nats://127.0.0.1:14222",
    read_merge_max_gap: int = 0,
    source_port_base: int = SOURCE_PORT,
    target_port_base: int = TARGET_PORT,
) -> Path:
    """创建统一联测配置树；输入全部来自仓库已发布样板。"""
    root = root.resolve()
    if root.exists():
        raise FileExistsError(root)
    if root == ROOT or ROOT in root.parents and root.parts[-1] != "dcems-t04":
        raise ValueError("t04 root must be a dedicated dcems-t04 directory")
    config = root / "configs"
    stage(config)

    template_dir = config / "templates"
    for obsolete in template_dir.glob("business_points_standard_v*.yaml"):
        if obsolete.name != "business_points_standard_v1.6.0.yaml":
            obsolete.unlink()

    # 本根内把物理点表的业务目录钉到与随机/统一表一致的1.6.0：
    # 相关业务点在1.5.0→1.6.0无语义差异，仅消除“混合目录版本”加载冲突。
    physical_table_path = template_dir / "point_tables" / "point_table_modbus_wtg_sim_v1.2.0.yaml"
    physical_doc = _read(physical_table_path)
    for source in physical_doc.get("config_sources", []):
        if source.get("config_type") == "business_points":
            source["file_version"] = "1.6.0"
    _write(physical_table_path, physical_doc)

    random_table = _read(template_dir / "point_tables" / "point_table_dcems_wtg_random_v1.0.0.yaml")
    physical_table = _read(template_dir / "point_tables" / "point_table_modbus_wtg_sim_v1.2.0.yaml")
    unified = _unified_point_table(random_table, physical_table["points"])
    _write(template_dir / "point_tables" / "point_table_dcems_wtg_unified_v1.0.0.yaml", unified)

    unified_model = _read(
        template_dir / "device_models" / "device_model_dcems_wtg_random_v1.0.0.yaml"
    )
    unified_model["template_id"] = "dcems_wtg_unified"
    unified_model["name"] = "DCEMS T04 unified device"
    unified_model["point_table"] = {"id": "dcems_wtg_unified", "version": VERSION}
    _write(
        template_dir / "device_models" / "device_model_dcems_wtg_unified_v1.0.0.yaml",
        unified_model,
    )

    instance_dir = config / "instances"
    # 设备实例：源侧 WTG01 物理、WTG02-10 随机；目标侧十机统一表。
    source_devices = _read(instance_dir / "device_dcems-smoke_v1.0.0.yaml")
    for index in range(1, 11):
        device_id = f"WTG{index:02d}"
        device = source_devices["devices"][device_id]
        device["group"] = "dcems-source"
        device["endpoint"]["port"] = source_port_base + index - 1
        device["overrides"] = {
            **device.get("overrides", {}),
            "protocol_options": {"read_merge_max_gap": read_merge_max_gap},
        }
        if index == 1:
            device["model"] = {"id": "wtg_sim_physical", "version": "1.2.0"}
            device["name"] = "T04 physical WTG01"
        else:
            device["model"] = {"id": "dcems_wtg_random", "version": VERSION}
    _write(instance_dir / "device_dcems-source_v1.0.0.yaml", source_devices)

    target_devices = deepcopy(source_devices)
    for index in range(1, 11):
        device_id = f"WTG{index:02d}"
        device = target_devices["devices"][device_id]
        device["group"] = "dcems-target"
        device["endpoint"]["port"] = target_port_base + index - 1
        device["model"] = {"id": "dcems_wtg_unified", "version": VERSION}
        device["name"] = f"T04 target WTG{index:02d}"
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

    # 仅保留统一场景：WTG01 已切换物理模型，旧随机场景会在加载期冲突。
    scenarios_path = instance_dir / "scenarios_dcems-smoke_v1.0.0.yaml"
    _write(scenarios_path, _scenario())

    # Gateway2（源/执行侧）：WTG01 具备 Commander 执行与 DG 仿真白名单。
    source_gateway = _read(config / "services" / "service_gateway-dcems-smoke_v1.0.0.yaml")
    source_gateway["service_id"] = "gateway-dcems-source"
    source_gateway["config_sources"]["instances"]["devices"] = "dcems-source"
    source_gateway["security"]["callers"] = [
        {
            "caller_id": "commander",
            "token_env": "WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN",
            "permissions": ["simulation_execute", "read"],
        },
        {
            "caller_id": "generator",
            "token_env": "WIND_HUB_DCE_GENERATOR_GATEWAY_TOKEN",
            "permissions": ["inject", "read"],
        },
        {
            "caller_id": "operator",
            "token_env": "WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN",
            "permissions": ["read", "lifecycle", "diagnose"],
        },
    ]
    for index, server in enumerate(source_gateway["servers"]):
        server["server_id"] = f"dcems-source-{index + 1:02d}"
        server["listen_port"] = source_port_base + index
        server["control"] = {
            "enabled": False,
            "target": {"kind": "none"},
            "mappings": [],
        }
    wtg01_source = next(s for s in source_gateway["servers"] if s["device_id"] == "WTG01")
    wtg01_source["control"] = {
        "enabled": True,
        "target": {
            "kind": "commander",
            "endpoint": f"127.0.0.1:{COMMANDER_GRPC}",
            "token_env": "WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN",
            "request_timeout_ms": 2000,
            "queue_capacity": 1024,
            "max_inflight": 1,
        },
        "simulation": {
            "endpoint": "127.0.0.1:52061",
            "token_env": "WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN",
            "scenario_id": "t04-unified-ten",
        },
        "mappings": [
            {"variable_id": "active_power_setpoint", "target_point_id": "active_power_setpoint"},
            {
                "variable_id": "reactive_power_setpoint",
                "target_point_id": "reactive_power_setpoint",
            },
        ],
    }
    _write(config / "services" / "service_gateway-dcems-source_v1.0.0.yaml", source_gateway)

    # Gateway1（目标/入口侧）：WTG01 承载 DCEMS FC16 控制入口。
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
        server["listen_port"] = target_port_base + index
        # Report expired Sink-fed values as STALE in Gateway ReadVariables.
        server["read_stale_after_ms"] = 5000
        server["control"] = {
            "enabled": False,
            "target": {"kind": "none"},
            "mappings": [],
        }
    wtg01_target = next(s for s in target_gateway["servers"] if s["device_id"] == "WTG01")
    wtg01_target["control"] = {
        "enabled": True,
        "target": {
            "kind": "commander",
            "endpoint": f"127.0.0.1:{COMMANDER_GRPC}",
            "token_env": "WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN",
            "request_timeout_ms": 2000,
            "queue_capacity": 1024,
            "max_inflight": 1,
        },
        "mappings": [
            {
                "variable_id": "active_power_setpoint",
                "target_point_id": "active_power_setpoint",
                "value_scale": 1000.0,
            },
            {
                "variable_id": "reactive_power_setpoint",
                "target_point_id": "reactive_power_setpoint",
                "value_scale": 1000.0,
            },
        ],
    }
    _write(config / "services" / "service_gateway-dcems-target_v1.0.0.yaml", target_gateway)

    # Commander：仅以 WTG01 为仿真目标，事件库与审计落在本根。
    commander = _read(
        ROOT
        / "deploy"
        / "config_roots"
        / "e2e-final"
        / "services"
        / "service_commander-e2e_v1.1.0.yaml"
    )
    commander["service_id"] = "commander-dcems-t04"
    commander["file_version"] = VERSION
    commander["grpc"]["listen_port"] = COMMANDER_GRPC
    commander["config_sources"]["instances"]["devices"] = "dcems-source"
    commander["simulation_targets"] = {
        "WTG01": {
            "gateway_id": "dcems-source-01",
            "endpoint": "127.0.0.1:52073",
            "token_env": "WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN",
        }
    }
    commander["repository"]["sqlite_path"] = str(root / "state" / "commander" / "events.db")
    commander["repository"]["max_storage_bytes"] = 1073741824
    commander["repository"]["reserved_storage_bytes"] = 134217728
    commander["audit"]["nats_servers"] = [nats_url]
    _write(config / "services" / "service_commander-dcems-t04_v1.0.0.yaml", commander)

    # Data Generator：源侧设备 + 统一场景；补充仿真执行调用方。
    generator = _read(config / "services" / "service_data-generator-dcems-smoke_v1.0.0.yaml")
    generator["service_id"] = "data-generator-dcems-t04"
    generator["config_sources"]["instances"] = {
        "site": "dcems-smoke",
        "devices": "dcems-source",
        "scenarios": "dcems-smoke",
    }
    generator["recording"]["directory"] = "state/generator-records"
    generator["security"]["callers"] = [
        {
            "caller_id": "cli",
            "token_env": "WIND_HUB_DCE_GENERATOR_CLI_TOKEN",
            "permissions": ["variables_read", "variables_update"],
        },
        {
            "caller_id": "gateway",
            "token_env": "WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN",
            "permissions": ["variables_read", "variables_update"],
        },
    ]
    _write(config / "services" / "service_data-generator-dcems-t04_v1.0.0.yaml", generator)

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
        sinks[sink_id] = {
            "template_id": "gateway_realtime",
            "template_version": "1.1.0",
            "connection": {
                "endpoint": "127.0.0.1:52075",
                "gateway_id": f"dcems-target-{index:02d}",
            },
            "overrides": {},
        }
        if index == 1:
            source_points = list(WTG01_DELIVERY_POINTS)
            mappings = [
                {
                    "source_device_id": device_id,
                    "source_point_id": source_point,
                    "variable_id": target_point,
                    **(
                        {"conversion": "int01_to_bool"}
                        if source_point == "fault_status"
                        else {}
                    ),
                }
                for source_point, target_point in WTG01_MAPPINGS
            ]
        else:
            source_points = list(RANDOM_POINTS)
            mappings = [
                {
                    "source_device_id": device_id,
                    "source_point_id": point,
                    "variable_id": point,
                }
                for point in RANDOM_POINTS
            ]
        deliveries[f"dcems_{device_id.lower()}"] = {
            "template_id": "realtime_power",
            "template_version": "1.1.0",
            "sink_id": sink_id,
            "desired_state": "START",
            "source": {
                "wind_farm_id": "dcems-smoke",
                "collector_id": "collector-dcems-realtime",
                "devices": [device_id],
                "points": source_points,
            },
            "target_mappings": mappings,
            "overrides": {},
        }
    _write(
        instance_dir / "sinks_dcems-measurement_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "revision": "dcems-t04-sinks-r1",
            "sinks": sinks,
        },
    )
    _write(
        instance_dir / "deliveries_dcems-measurement_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "revision": "dcems-t04-deliveries-r1",
            "deliveries": deliveries,
        },
    )

    _write(
        config / "deployments" / "deployment_dcems-t04_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": VERSION,
            "deployment_id": DEPLOYMENT,
            "services": {
                name: {"config_id": name, "file_version": version}
                for name, version in (
                    ("gateway-dcems-source", VERSION),
                    ("gateway-dcems-target", VERSION),
                    ("commander-dcems-t04", VERSION),
                    ("data-generator-dcems-t04", VERSION),
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
    for obsolete in (
        "services/service_gateway-dcems-smoke_v1.0.0.yaml",
        "services/service_data-generator-dcems-smoke_v1.0.0.yaml",
        "deployments/deployment_dcems-smoke_v1.0.0.yaml",
        "instances/device_dcems-smoke_v1.0.0.yaml",
    ):
        (config / obsolete).unlink()

    # 正式 CLI 编排：六服务统一启停，工业端口探针指向两侧 WTG01。
    import sys

    runtime = root / "state"
    process_definitions: dict[str, tuple[str, str, list[str], str | None]] = {
        "gateway": (
            "industrial_gateway.main",
            "service_gateway-dcems-source_v1.0.0.yaml",
            ["--project-root", str(config)],
            "WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN",
        ),
        "gateway-target": (
            "industrial_gateway.main",
            "service_gateway-dcems-target_v1.0.0.yaml",
            ["--project-root", str(config)],
            "WIND_HUB_DCE_TARGET_OPERATOR_TOKEN",
        ),
        "commander": (
            "commander",
            "service_commander-dcems-t04_v1.0.0.yaml",
            [],
            "WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN",
        ),
        "data-generator": (
            "data_generator.main",
            "service_data-generator-dcems-t04_v1.0.0.yaml",
            [],
            "WIND_HUB_DCE_GENERATOR_CLI_TOKEN",
        ),
        "collector": (
            "collector.main",
            "service_collector-dcems-realtime_v2.0.0.yaml",
            [
                "--state-directory",
                str(runtime / "collector"),
                "--collector-id",
                "collector-dcems-realtime",
                "--grpc-host",
                "127.0.0.1",
                "--grpc-port",
                "52051",
            ],
            None,
        ),
        "sink": (
            "sink_service.main",
            "service_sink-dcems-measurement_v2.0.0.yaml",
            [],
            "WIND_HUB_SINK_CLI_TOKEN",
        ),
    }
    endpoints = {
        "gateway": "127.0.0.1:52073",
        "gateway-target": "127.0.0.1:52074",
        "commander": f"127.0.0.1:{COMMANDER_GRPC}",
        "data-generator": "127.0.0.1:52061",
        "collector": "127.0.0.1:52051",
        "sink": "127.0.0.1:52053",
    }
    services: dict[str, Any] = {}
    for name, (module, filename, additional, token_env) in process_definitions.items():
        argv = [sys.executable, "-m", module]
        if name == "sink":
            argv.extend(
                [
                    "--config-root",
                    str(root),
                    "--service-file",
                    f"configs/services/{filename}",
                ]
            )
        else:
            argv.extend(["--config", str(config / "services" / filename), *additional])
        entry: dict[str, Any] = {
            "grpc_endpoint": endpoints[name],
            "manager": "process",
            "target": name,
            "process": {
                "argv": argv,
                "cwd": str(ROOT),
                "state_dir": str(runtime / "cli-processes"),
                "log_file": str(runtime / f"{name}.log"),
            },
        }
        if token_env is not None:
            entry["token_env"] = token_env
        services[name] = entry
    cli = {
        "schema_version": 1,
        "file_version": VERSION,
        "service_id": "cli-dcems-t04",
        "cli": {
            "prompt": "dcems-t04> ",
            "output_format": "json",
            "timeout_seconds": 30,
            "history": False,
            "max_display_rows": 1000,
            "poll_interval_ms": 1000,
            "poll_reconnect_attempts": 3,
            "snapshot_max_bytes": 67108864,
            "diff_max_file_bytes": 1048576,
            "diff_max_output_lines": 10000,
        },
        "services": services,
        "network_probe": {
            "max_hosts": 1024,
            "max_concurrency": 64,
            "timeout_seconds": 1,
            "industrial_tcp_ports": [target_port_base, source_port_base],
        },
    }
    _write(config / "services" / "service_cli-dcems-t04_v1.0.0.yaml", cli)
    _write(runtime / "cli.yaml", cli)
    return config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:14222")
    parser.add_argument("--read-merge-max-gap", type=int, default=0)
    parser.add_argument("--source-port-base", type=int, default=SOURCE_PORT)
    parser.add_argument("--target-port-base", type=int, default=TARGET_PORT)
    args = parser.parse_args()
    print(
        prepare(
            args.output,
            nats_url=args.nats_url,
            read_merge_max_gap=args.read_merge_max_gap,
            source_port_base=args.source_port_base,
            target_port_base=args.target_port_base,
        )
    )


if __name__ == "__main__":
    main()
