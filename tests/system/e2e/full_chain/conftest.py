"""Full-chain E2E（跨模块测试设计 v0_27 §7）：真实六服务 × 真实中间件 × 全程 CLI 生命周期。

统一约束（设计 §5.4 / §7.4）：

- NATS/JetStream、Redis、Kafka 由测试基础设施（r5 真实 Docker 栈）启动清理；
- Gateway、Data Generator、双 Collector、Sink、Commander 六个业务进程全部经
  正式 CLI ``service start/stop/restart/status`` 的原生 Linux 进程管理器管理；
- 全部业务命令（Generation/Task/Delivery/变量更新/控制）经 CLI；无 CLI 叶子
  对应的正式 RPC（如 Generator ``GetInputExecution``、Gateway
  ``ExecuteSimulationCommand``）由真实 mTLS 客户端补齐并在矩阵注明；
- 完整物理链路：DG 五设备物理模型 → Gateway 变量存储（授权 inject）→
  Gateway 下行 Modbus Server（隔离的 E2E 设备端口）→ 双 Collector（Realtime/
  Reliable）→ NATS 双通道 → Sink 四出口（Gateway/Modbus/Redis/Kafka）；
- 源真值来自 DG Recording JSONL（逐 generation 落盘），下游读回全部独立
  客户端（pymodbus、RESP2、aiokafka、Gateway gRPC），不读被测进程内部状态。

设备清单双轨：DG 与 Gateway/Collector/Commander 分别使用 Deployment 精确选择的
版本化 Device Instance；采集周期由独立 Collector Task Instance 声明。WTG E2E
点表单独追加 Sink 回写目标，物理输出点仍沿用出厂地址，避免将测试回写点误作为
DG 必须生成的物理量。Gateway/Generator/Commander 使用现行调用方令牌鉴权，
Collector 管理面继续使用独立 mTLS。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from tests.cli.test_r1f3_strict_cli_e2e import (
    ADMIN_TOKEN,
    SERVICES,
    StrictCliE2E,
    _provision_commander_audit,
    _write_cli,
)
from tests.fixtures.services import r5_stack
from tests.sink_service._stage1_fixtures import DELIVERIES_INSTANCE, SINKS_INSTANCE
from tests.sink_service._stage1_fixtures import SERVICE_FILE as SINK_SERVICE_FILE
from tests.sink_service.system.conftest import SinkSystemHarness
from tests.support.collector_config_tree import _dump
from tests.support.gateway import reserve_port, write_mtls_pki

SRC = Path(__file__).resolve().parents[4] / "src"
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_CONFIGS = REPO_ROOT / "configs"

#: 出厂 site.yaml 的站点身份——NATS 批次与 Sink source 过滤器必须与此一致。
FARM = "example_site"
ADMIN_TOKEN = ADMIN_TOKEN
SIM_DEVICES = ("WTG_SIM01", "PV_SIM01", "BESS_SIM01", "GRID_SIM01", "PPC_SIM01")
REPLAY_DEVICE = "GW02"
E2E_DEVICES = (*SIM_DEVICES, REPLAY_DEVICE)

#: Sink GatewayOutput 的专用目标变量（寄存器随出厂 sim WTG 点表副本追加，
#: 不与 DG 注入变量冲突）。
SINK_TARGET_POINT = "e2e_sink_target"
GOLDEN_DEVICES = {"PV_SIM01": "active_power", "BESS_SIM01": "soc"}
E2E_POINT_TABLES = {
    "WTG_SIM01": ("modbus_wtg_sim", "modbus_wtg_sim_e2e"),
    "PV_SIM01": ("modbus_pv_sim", "modbus_pv_sim_e2e"),
    "BESS_SIM01": ("modbus_bess_sim", "modbus_bess_sim_e2e"),
    "GRID_SIM01": ("modbus_grid_sim", "modbus_grid_sim_e2e"),
    "PPC_SIM01": ("modbus_plant_controller_sim", "modbus_plant_controller_sim_e2e"),
}
SINK_TARGET_DEVICES = {"WTG_SIM01", "PV_SIM01", "BESS_SIM01"}
SIM_DEVICE_INSTANCE = "full-chain-sim"
E2E_DEVICE_INSTANCE = "full-chain-e2e"
TASK_INSTANCE = "full-chain"
SCENARIO_INSTANCE = "full-chain"
DEPLOYMENT_ID = "full-chain"
SERVICE_VERSION = "1.0.0"
BUSINESS_POINTS_VERSION = "1.5.1"
GATEWAY_CLI_TOKEN_ENV = "WIND_HUB_TEST_GATEWAY_CLI_TOKEN"
GENERATOR_CLI_TOKEN_ENV = "WIND_HUB_TEST_DG_CLI_TOKEN"
GENERATOR_GATEWAY_TOKEN_ENV = "WIND_HUB_TEST_GENERATOR_GATEWAY_TOKEN"
GATEWAY_GENERATOR_TOKEN_ENV = "WIND_HUB_TEST_GATEWAY_GENERATOR_TOKEN"
COMMANDER_GATEWAY_TOKEN_ENV = "WIND_HUB_TEST_COMMANDER_GATEWAY_TOKEN"
GATEWAY_COMMANDER_TOKEN_ENV = "WIND_HUB_TEST_GATEWAY_COMMANDER_TOKEN"
COMMANDER_OPS_TOKEN_ENV = "WIND_HUB_TEST_COMMANDER_OPS_TOKEN"
COMMANDER_MAINTENANCE_TOKEN_ENV = "WIND_HUB_TEST_COMMANDER_MAINTENANCE_TOKEN"
TOKEN_ENVS = (
    GATEWAY_CLI_TOKEN_ENV,
    GENERATOR_CLI_TOKEN_ENV,
    GENERATOR_GATEWAY_TOKEN_ENV,
    GATEWAY_GENERATOR_TOKEN_ENV,
    COMMANDER_GATEWAY_TOKEN_ENV,
    GATEWAY_COMMANDER_TOKEN_ENV,
    COMMANDER_OPS_TOKEN_ENV,
    COMMANDER_MAINTENANCE_TOKEN_ENV,
)


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path}: expected mapping"
    return document


def _factory_variable_mappings() -> dict[str, dict[str, str]]:
    """Load all source-to-Gateway point identities from the shipped scenario."""
    shipped = _load(REPO_CONFIGS / "instances" / "scenarios_sim_v1.0.4.yaml")
    routes = shipped["scenarios"][0]["gateways"]
    assert isinstance(routes, list), "factory Gateway routes are missing"

    devices = _load(REPO_CONFIGS / "instances" / "device_sim_v1.4.0.yaml")["devices"]
    mappings: dict[str, dict[str, str]] = {}
    for route in routes:
        device_id = route["device_id"]
        assert (
            device_id in SIM_DEVICES and device_id not in mappings
        ), f"unexpected or duplicate factory Gateway route: {device_id}"
        raw_mapping = route["variable_mapping"]
        assert isinstance(raw_mapping, dict), f"{device_id}: missing explicit variable_mapping"
        mapping = dict(raw_mapping)
        assert all(
            isinstance(point, str) and isinstance(target, str) and target
            for point, target in mapping.items()
        ), f"{device_id}: invalid Gateway variable mapping"

        model_ref = devices[device_id]["model"]
        model = _load(
            REPO_CONFIGS
            / "templates"
            / "device_models"
            / f"device_model_{model_ref['id']}_v{model_ref['version']}.yaml"
        )
        table_ref = model["point_table"]
        table = _load(
            REPO_CONFIGS
            / "templates"
            / "point_tables"
            / f"point_table_{table_ref['id']}_v{table_ref['version']}.yaml"
        )
        declared_points = set(table["points"])
        assert (
            set(mapping) == declared_points
        ), f"{device_id}: factory source-point mapping differs from shared table"
        assert (
            set(mapping.values()) <= declared_points
        ), f"{device_id}: factory Gateway target is absent from shared table"
        mappings[device_id] = mapping

    assert set(mappings) == set(SIM_DEVICES), "factory scenario does not cover five devices"
    return mappings


def _weather_block(*, turbulence: float = 0.0, mean: float = 10.0) -> dict[str, Any]:
    """E2E 物理场景天气：固定种子、可复现；TI=0 时风速恒等于均值。"""
    return {
        "site-a": {
            "wind": {
                "wind_seed": 42,
                "wind_correlation_time_s": 10.0,
                "wind_speed_mean": mean,
                "turbulence_intensity": turbulence,
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
                "temperature_sigma_k": 0.5,
                "phase_rad": 0.0,
            },
        }
    }


def _physical_scenario(
    scenario_id: str,
    version: str,
    gateway_endpoint: str,
    *,
    interval: float,
    weather: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """五设备物理场景（wind-plant-physical 契约，天气与步长参数化）。"""
    factory_mappings = _factory_variable_mappings()
    gateways = [
        {
            "gateway_id": f"gw-{device_id.lower()}",
            "endpoint": gateway_endpoint,
            "device_id": device_id,
            "variable_mapping": factory_mappings[device_id],
        }
        for device_id in SIM_DEVICES
    ]
    return {
        "scenario_id": scenario_id,
        "version": version,
        "description": f"full-chain E2E physical scenario {scenario_id}",
        "source": {"kind": "simulation", "interval_seconds": interval},
        "weather": weather or _weather_block(),
        "devices": [
            {"device_id": device_id, "weather_ref": "site-a", "controller_mode": "ppc"}
            for device_id in SIM_DEVICES[:4]
        ]
        + [{"device_id": "PPC_SIM01"}],
        "gateways": gateways,
    }


def _plant_root(base: Path) -> Path:
    """复制出厂 configs（唯一允许的共享模板来源）作为 E2E 共享配置根。"""
    root = base / "plant-configs"
    for name in ("instances", "templates", "history"):
        shutil.copytree(REPO_CONFIGS / name, root / name)
    (root / "services").mkdir()
    return root


def _write_sim_devices(root: Path, modbus_ports: dict[str, int]) -> None:
    """Versioned Generator Device Instance with isolated Modbus endpoints."""
    document = _load(REPO_CONFIGS / "instances" / "device_sim_v1.4.0.yaml")
    document["file_version"] = SERVICE_VERSION
    for device_id in SIM_DEVICES:
        port = modbus_ports[device_id]
        document["devices"][device_id]["endpoint"]["port"] = port
    document["devices"][REPLAY_DEVICE] = {
        "name": "GW02 demo.csv replay device",
        "enabled": True,
        "group": "full_chain_replay",
        "model": {"id": "dg_gw02_replay", "version": SERVICE_VERSION},
        "endpoint": {"host": "127.0.0.1", "port": modbus_ports[REPLAY_DEVICE]},
        "overrides": {},
    }
    _dump(root / "instances" / f"device_{SIM_DEVICE_INSTANCE}_v{SERVICE_VERSION}.yaml", document)


def _write_e2e_devices(root: Path) -> None:
    """Versioned acquisition/control Device Instance with task-assignment groups."""
    document = _load(root / "instances" / f"device_{SIM_DEVICE_INSTANCE}_v{SERVICE_VERSION}.yaml")
    devices = {}
    for device_id in E2E_DEVICES:
        source = document["devices"][device_id]
        model_id = "e2e_gw02_replay" if device_id == REPLAY_DEVICE else f"e2e_{device_id.lower()}"
        devices[device_id] = {
            "name": source["name"],
            "enabled": source["enabled"],
            "group": f"full_chain_{device_id.lower()}",
            "model": {"id": model_id, "version": SERVICE_VERSION},
            "endpoint": {
                "host": "127.0.0.1",
                "port": int(source["endpoint"]["port"]),
            },
            "overrides": {},
        }
    _dump(
        root / "instances" / f"device_{E2E_DEVICE_INSTANCE}_v{SERVICE_VERSION}.yaml",
        {"schema_version": 1, "file_version": SERVICE_VERSION, "devices": devices},
    )


def _write_e2e_models_and_tasks(root: Path, intervals: dict[str, float] | None = None) -> None:
    """Create pinned E2E models and a separate versioned Collector Task Instance."""
    sim_devices = _load(
        root / "instances" / f"device_{SIM_DEVICE_INSTANCE}_v{SERVICE_VERSION}.yaml"
    )
    for device_id in SIM_DEVICES:
        model_ref = sim_devices["devices"][device_id]["model"]
        document = _load(
            REPO_CONFIGS
            / "templates"
            / "device_models"
            / f"device_model_{model_ref['id']}_v{model_ref['version']}.yaml"
        )
        document["template_id"] = f"e2e_{device_id.lower()}"
        document["file_version"] = SERVICE_VERSION
        if device_id in E2E_POINT_TABLES:
            # Sink 回写点只属于 Gateway/Collector 投影，不要求 DG 物理模型生成。
            document["point_table"] = {
                "id": E2E_POINT_TABLES[device_id][1],
                "version": SERVICE_VERSION,
            }
        document.pop("task", None)
        _dump(
            root
            / "templates"
            / "device_models"
            / f"device_model_e2e_{device_id.lower()}_v{SERVICE_VERSION}.yaml",
            document,
        )
    for prefix in ("dg", "e2e"):
        model_id = f"{prefix}_gw02_replay"
        table_id = f"modbus_{model_id}"
        _dump(
            root
            / "templates"
            / "device_models"
            / f"device_model_{model_id}_v{SERVICE_VERSION}.yaml",
            {
                "schema_version": 1,
                "file_version": SERVICE_VERSION,
                "template_id": model_id,
                "name": "GW02 demo.csv replay device",
                "device_type_id": "turbine",
                "protocol": {"id": "modbus_tcp", "version": "1.1.0"},
                "point_table": {"id": table_id, "version": SERVICE_VERSION},
            },
        )
    tasks = {
        f"full_chain_{device_id.lower()}": {
            "group": f"full_chain_{device_id.lower()}",
            "point_group": "all",
            "interval_ms": round((intervals or {}).get(device_id, 0.2) * 1000),
            "desired_state": "STOP",
        }
        for device_id in E2E_DEVICES
    }
    _dump(
        root / "instances" / f"collector_tasks_{TASK_INSTANCE}_v{SERVICE_VERSION}.yaml",
        {"schema_version": 1, "file_version": SERVICE_VERSION, "tasks": tasks},
    )
    for prefix, business_version in (
        ("dg", BUSINESS_POINTS_VERSION),
        ("e2e", BUSINESS_POINTS_VERSION),
    ):
        table_id = f"modbus_{prefix}_gw02_replay"
        _dump(
            root / "templates" / "point_tables" / f"point_table_{table_id}_v{SERVICE_VERSION}.yaml",
            {
                "schema_version": 1,
                "file_version": SERVICE_VERSION,
                "template_id": table_id,
                "protocol": "modbus",
                "config_sources": [
                    {
                        "config_type": "units",
                        "config_id": "standard",
                        "file_version": "1.2.0",
                    },
                    {
                        "config_type": "business_points",
                        "config_id": "standard",
                        "file_version": business_version,
                    },
                ],
                "points": {
                    "a02_dm_pitchangle1": {
                        "business_point_id": "pitch_angle",
                        "point_groups": ["all", "pitch"],
                        "address": {
                            "register_type": "input",
                            "address": 0,
                            "count": 2,
                            "slave_id": 1,
                        },
                        "data_type": "float32",
                        "unit": "degree",
                        "scale": 1.0,
                        "offset": 0.0,
                        "description": "E2E projection of configs/history/demo.csv GW02 point",
                    }
                },
            },
        )
    # 三类物理设备使用各自扩展点表，避免 Sink 回写地址占用 DG 输入寄存器。
    for device_id, (source_id, target_id) in E2E_POINT_TABLES.items():
        model_ref = sim_devices["devices"][device_id]["model"]
        model = _load(
            REPO_CONFIGS
            / "templates"
            / "device_models"
            / f"device_model_{model_ref['id']}_v{model_ref['version']}.yaml"
        )
        source_version = model["point_table"]["version"]
        point_table = (
            REPO_CONFIGS
            / "templates"
            / "point_tables"
            / f"point_table_{source_id}_v{source_version}.yaml"
        )
        document = _load(point_table)
        document["template_id"] = target_id
        document["file_version"] = SERVICE_VERSION
        for source in document["config_sources"]:
            if source["config_type"] == "business_points":
                source["config_id"] = "standard"
                source["file_version"] = BUSINESS_POINTS_VERSION
        if device_id in SINK_TARGET_DEVICES:
            document["points"][SINK_TARGET_POINT] = {
                "business_point_id": SINK_TARGET_POINT,
                "point_groups": ["all"],
                "address": {
                    "register_type": "holding",
                    "address": 400,
                    "count": 2,
                    "slave_id": 1,
                },
                "data_type": "float32",
                "unit": "none",
                "scale": 1.0,
                "offset": 0.0,
                "description": "E2E Sink GatewayOutput target",
            }
        _dump(
            root
            / "templates"
            / "point_tables"
            / f"point_table_{target_id}_v{SERVICE_VERSION}.yaml",
            document,
        )
    business = _load(root / "templates" / "business_points_standard_v1.5.0.yaml")
    business["file_version"] = BUSINESS_POINTS_VERSION
    business["business_points"][SINK_TARGET_POINT] = {"data_type": "float32", "unit": "none"}
    _dump(
        root / "templates" / f"business_points_standard_v{BUSINESS_POINTS_VERSION}.yaml",
        business,
    )
    for table_path in (root / "templates" / "point_tables").glob("*.yaml"):
        table = _load(table_path)
        changed = False
        for source in table.get("config_sources", []):
            if source.get("config_type") == "business_points":
                source["file_version"] = BUSINESS_POINTS_VERSION
                changed = True
        if changed:
            _dump(table_path, table)
    for prefix, selected_version in (
        ("business_points_standard", BUSINESS_POINTS_VERSION),
        ("units_standard", "1.2.0"),
    ):
        for catalog in (root / "templates").glob(f"{prefix}_v*.yaml"):
            if catalog.name != f"{prefix}_v{selected_version}.yaml":
                catalog.unlink()


def _write_collector_service(root: Path, service_id: str, *, nats_url: str, delivery: str) -> Path:
    shipped = "service_collector-sim_v2.0.0.yaml"
    if delivery == "reliable":
        shipped = "service_collector-reliable_v2.0.0.yaml"
    document = _load(REPO_CONFIGS / "services" / shipped)
    document["service_id"] = service_id
    document["file_version"] = SERVICE_VERSION
    document["config_sources"] = {
        "instances": {
            "site": "example_site",
            "devices": E2E_DEVICE_INSTANCE,
            "tasks": TASK_INSTANCE,
        }
    }
    document["messaging"]["servers"] = [nats_url]
    document["messaging"]["delivery"] = delivery
    document["runtime"].update(
        {"connect_timeout": 5.0, "read_timeout": 5.0, "shutdown_timeout": 10.0}
    )
    document["watchdog"] = {"interval": 0.5, "health_timeout": 0.5}
    path = root / "services" / f"service_{service_id}_v{SERVICE_VERSION}.yaml"
    _dump(path, document)
    return path


def _write_gateway_service(root: Path, *, grpc_port: int) -> Path:
    document = _load(REPO_CONFIGS / "services" / "service_gateway-e2e-source_v1.1.3.yaml")
    service_id = "gateway-full-chain"
    document["service_id"] = service_id
    document["file_version"] = SERVICE_VERSION
    document["config_sources"] = {"instances": {"devices": E2E_DEVICE_INSTANCE}}
    document["grpc"]["listen_port"] = grpc_port
    document["security"] = {
        "callers": [
            {
                "caller_id": "operator",
                "token_env": GATEWAY_CLI_TOKEN_ENV,
                "permissions": ["read", "lifecycle", "diagnose"],
            },
            {
                "caller_id": "generator",
                "token_env": GENERATOR_GATEWAY_TOKEN_ENV,
                "permissions": ["inject", "read"],
            },
            {
                "caller_id": "commander",
                "token_env": COMMANDER_GATEWAY_TOKEN_ENV,
                "permissions": ["simulation_execute", "read"],
            },
            {
                "caller_id": "sink",
                "token_env": "WIND_HUB_GATEWAY_SINK_TOKEN",
                "permissions": ["inject"],
            },
        ]
    }
    path = root / "services" / f"service_{service_id}_v{SERVICE_VERSION}.yaml"
    _dump(path, document)
    return path


def _write_dg_service(
    root: Path,
    *,
    bind: str,
    scenarios: list[dict[str, Any]],
) -> Path:
    document = _load(REPO_CONFIGS / "services" / "service_data-generator-sim_v1.3.2.yaml")
    service_id = "data-generator-full-chain"
    document["service_id"] = service_id
    document["file_version"] = SERVICE_VERSION
    document["config_sources"] = {
        "instances": {
            "site": "example_site",
            "devices": SIM_DEVICE_INSTANCE,
            "scenarios": SCENARIO_INSTANCE,
        }
    }
    document["service"]["grpc_bind"] = bind
    document["output"]["gateway_token_env"] = GENERATOR_GATEWAY_TOKEN_ENV
    document["security"] = {
        "callers": [
            {
                "caller_id": "cli",
                "token_env": GENERATOR_CLI_TOKEN_ENV,
                "permissions": ["variables_read", "variables_update", "generation_manage"],
            },
            {
                "caller_id": "gateway",
                "token_env": GATEWAY_GENERATOR_TOKEN_ENV,
                "permissions": ["variables_read", "variables_update"],
            },
        ]
    }
    document["recording"] = {"enabled": True, "directory": "recording"}
    _dump(
        root / "instances" / f"scenarios_{SCENARIO_INSTANCE}_v{SERVICE_VERSION}.yaml",
        {"schema_version": 1, "file_version": SERVICE_VERSION, "scenarios": scenarios},
    )
    path = root / "services" / f"service_{service_id}_v{SERVICE_VERSION}.yaml"
    _dump(path, document)
    return path


def _write_commander_service(
    root: Path,
    *,
    grpc_port: int,
    gateway_endpoint: str,
    nats_url: str,
    audit_stream: str,
    audit_subject: str,
) -> Path:
    document = _load(REPO_CONFIGS / "services" / "service_commander-e2e_v1.1.0.yaml")
    service_id = "commander-full-chain"
    document["service_id"] = service_id
    document["file_version"] = SERVICE_VERSION
    document["config_sources"] = {"instances": {"devices": E2E_DEVICE_INSTANCE}}
    document["grpc"]["listen_port"] = grpc_port
    document["security"] = {
        "callers": [
            {
                "caller_id": "ops",
                "token_env": COMMANDER_OPS_TOKEN_ENV,
                "permissions": ["query", "control", "reload", "recover"],
            },
            {
                "caller_id": "maintenance",
                "token_env": COMMANDER_MAINTENANCE_TOKEN_ENV,
                "permissions": ["query", "reload", "recover"],
            },
            {
                "caller_id": "gateway",
                "token_env": GATEWAY_COMMANDER_TOKEN_ENV,
                "permissions": ["query", "control"],
            },
        ]
    }
    document["simulation_targets"] = {
        "PPC_SIM01": {
            "gateway_id": "gw-ppc_sim01",
            "endpoint": gateway_endpoint,
            "token_env": COMMANDER_GATEWAY_TOKEN_ENV,
        }
    }
    state_dir = root.parent / "commander-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    document["repository"]["sqlite_path"] = str(state_dir / "events.db")
    document["repository"]["max_storage_bytes"] = 1073741824
    document["repository"]["reserved_storage_bytes"] = 67108864
    document["audit"]["nats_servers"] = [nats_url]
    document["audit"]["stream"] = audit_stream
    document["audit"]["subject"] = audit_subject
    path = root / "services" / f"service_{service_id}_v{SERVICE_VERSION}.yaml"
    _dump(path, document)
    return path


def _write_deployment(root: Path) -> None:
    service_ids = (
        "gateway-full-chain",
        "data-generator-full-chain",
        "collector-full-chain-rt",
        "collector-full-chain-reliable",
        "commander-full-chain",
    )
    document = {
        "schema_version": 1,
        "file_version": SERVICE_VERSION,
        "deployment_id": DEPLOYMENT_ID,
        "services": {
            service_id: {"config_id": service_id, "file_version": SERVICE_VERSION}
            for service_id in service_ids
        },
        "instances": {
            "site": {"example_site": {"file_version": "1.1.0"}},
            "device": {
                SIM_DEVICE_INSTANCE: {"file_version": SERVICE_VERSION},
                E2E_DEVICE_INSTANCE: {"file_version": SERVICE_VERSION},
            },
            "collector_tasks": {TASK_INSTANCE: {"file_version": SERVICE_VERSION}},
            "scenario": {SCENARIO_INSTANCE: {"file_version": SERVICE_VERSION}},
        },
    }
    _dump(
        root / "deployments" / f"deployment_{DEPLOYMENT_ID}_v{SERVICE_VERSION}.yaml",
        document,
    )


class FullChainHarness:
    """Session 级六服务现场：只经 CLI 操作业务面，观测全部在系统边界。"""

    def __init__(self, root: Path, stack: r5_stack.R5Stack, cli: StrictCliE2E) -> None:
        self.root = root
        self.stack = stack
        self.cli = cli
        self.token = uuid4().hex[:8]
        self.rt_collector = f"collector-rt-{self.token}"
        self.reliable_collector = f"collector-hist-{self.token}"
        self.ports: dict[str, int] = {}
        self.endpoints: dict[str, str] = {}
        self.modbus_ports: dict[str, int] = {}
        self.gw_grpc_endpoint = ""
        self.gateway_sink_token = ""
        self.cli_rt: Path = root / "cli-realtime.yaml"
        self.cli_rel: Path = root / "cli-reliable.yaml"
        self.sink: SinkSystemHarness | None = None
        self.pki: dict[str, Any] = {}
        self.tokens: dict[str, str] = {}
        self.plant: Path = root / "plant-configs"
        # Data Generator resolves recording.directory against its service root,
        # which is the Full-chain temporary root rather than plant-configs.
        self.recording_dir = root / "recording"
        self.generation_id: str | None = None
        self.evidence_path = root / "evidence.jsonl"
        self.sink_modbus_port = 0
        self.sink_modbus_ports: dict[str, int] = {}
        self.source_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        self.source_diff_sha256 = hashlib.sha256(
            subprocess.check_output(["git", "diff", "--binary"], cwd=REPO_ROOT)
        ).hexdigest()

    # ---------------------------- evidence ----------------------------
    def _config_sha256(self) -> str:
        digest = hashlib.sha256()
        for root in (self.plant, self.root / "sink"):
            for path in sorted(root.rglob("*.yaml")):
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def record(self, case: str, **payload: Any) -> None:
        entry = {
            "case_id": case,
            "time": time.time(),
            "head": self.source_head,
            "worktree_diff_sha256": self.source_diff_sha256,
            "config_sha256": self._config_sha256(),
            **payload,
        }
        with self.evidence_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    # ------------------------------ CLI -------------------------------
    async def cli_call(
        self,
        config: Path,
        *args: str,
        timeout: float = 45,
        check: bool = True,
        token: str | None = ADMIN_TOKEN,
    ) -> dict[str, Any]:
        # Test cases select a physical device; resolve its selected Task Instance
        # identity through the live Collector before issuing StartTask/StopTask.
        if (
            len(args) >= 3
            and args[0] == "collector"
            and args[1] in {"start", "stop"}
            and args[2] in SIM_DEVICES
        ):
            device = await self.cli.cli(
                config,
                "collector",
                "device",
                args[2],
                timeout=timeout,
                check=True,
                token=ADMIN_TOKEN,
            )
            task_id = device["data"]["device"]["task_id"]
            assert task_id, f"Collector has no configured Task for {args[2]}"
            args = (*args[:2], task_id, *args[3:])
        return await self.cli.cli(config, *args, timeout=timeout, check=check, token=token)

    async def poll(
        self,
        config: Path,
        *args: str,
        predicate: Callable[[Any], bool],
        timeout: float = 60,
    ) -> Any:
        return await self.cli.poll_cli(config, *args, predicate=predicate, timeout=timeout)

    async def service(self, operation: str, name: str) -> dict[str, Any]:
        return await self.cli.service(self.cli_rt, operation, name)

    # --------------------------- recording -----------------------------
    def recording_path(self, generation_id: str) -> Path:
        return self.recording_dir / f"{generation_id}.jsonl"

    def read_recording(self, generation_id: str) -> list[dict[str, Any]]:
        path = self.recording_path(generation_id)
        assert path.is_file(), f"recording JSONL missing: {path}"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert rows, f"recording JSONL empty: {path}"
        return rows

    # --------------------------- redis key -----------------------------
    def redis_key(self, device_id: str, point_id: str, *, collector: str | None = None) -> str:
        assert self.sink is not None
        return ":".join(
            (self.sink.namespace, FARM, collector or self.rt_collector, device_id, point_id)
        )


def _build_scenarios(gateway_endpoint: str) -> list[dict[str, Any]]:
    scenarios = [
        _physical_scenario("wind-plant-physical", "1.0.0", gateway_endpoint, interval=0.05),
        _physical_scenario("e2e-wind-100ms", "1.0.0", gateway_endpoint, interval=0.1),
        _physical_scenario(
            "e2e-wind-seeded",
            "1.0.0",
            gateway_endpoint,
            interval=0.05,
            weather=_weather_block(turbulence=0.12),
        ),
    ]
    for speed in (1, 2):
        scenarios.append(
            {
                "scenario_id": f"e2e-replay-{speed}x",
                "version": "1.0.0",
                "description": "Full-chain WTG CSV replay",
                "source": {
                    "kind": "replay",
                    "history_reader": "csv",
                    "file": "history/demo.csv",
                    "start_time": "2026-01-01T00:00:00Z",
                    "end_time": "2026-01-01T00:00:03Z",
                    "speed": speed,
                    "loop": False,
                },
                "devices": [
                    {"device_id": REPLAY_DEVICE, "points": [{"point_id": "a02_dm_pitchangle1"}]}
                ],
                "gateways": [
                    {
                        "gateway_id": "gw-gw02",
                        "endpoint": gateway_endpoint,
                        "device_id": REPLAY_DEVICE,
                        "variable_mapping": {"a02_dm_pitchangle1": "a02_dm_pitchangle1"},
                    }
                ],
            }
        )
    # PV 短日场景：0/800/1000 三个清晰边界，环境温度固定 25°C。
    for scenario_id, cloud in (
        ("e2e-pv-0", 0.0),
        ("e2e-pv-800", 0.8),
        ("e2e-pv-1000", 1.0),
    ):
        document = _physical_scenario(scenario_id, "1.0.0", gateway_endpoint, interval=0.05)
        irradiance = document["weather"]["site-a"]["irradiance"]
        irradiance.update({"sunrise_s": 0, "sunset_s": 40, "cloud_factor_target": cloud})
        temperature = document["weather"]["site-a"]["ambient_temperature"]
        temperature.update(
            {
                "temperature_mean_c": 25.0,
                "diurnal_amplitude_k": 0.0,
                "temperature_sigma_k": 0.0,
            }
        )
        scenarios.append(document)
    return scenarios


def _build_and_start(
    root: Path,
    intervals: dict[str, float] | None = None,
    *,
    bess_initial_soc: float | None = None,
) -> FullChainHarness:
    stack = asyncio.run(_up_stack_async())
    cli = StrictCliE2E(root)
    harness = FullChainHarness(root, stack, cli)
    ports = {name: reserve_port() for name in SERVICES}
    ports["gateway_sink"] = reserve_port()
    gateway_sink_token = os.urandom(32).hex()
    harness.gateway_sink_token = gateway_sink_token
    harness.ports = ports
    harness.endpoints = {name: f"127.0.0.1:{port}" for name, port in ports.items()}
    harness.modbus_ports = {device_id: reserve_port() for device_id in E2E_DEVICES}
    harness.gw_grpc_endpoint = harness.endpoints["gateway"]
    harness.sink_modbus_port = reserve_port()
    harness.sink_modbus_ports = {"WTG_SIM01": harness.sink_modbus_port}
    harness.sink_modbus_ports.update({device: reserve_port() for device in GOLDEN_DEVICES})

    plant = _plant_root(root)
    harness.plant = plant
    _write_sim_devices(plant, harness.modbus_ports)
    _write_e2e_models_and_tasks(plant, intervals)
    _write_e2e_devices(plant)
    if bess_initial_soc is not None:
        bess_model = (
            plant / "templates" / "device_models" / "device_model_bess_sim_physical_v1.2.0.yaml"
        )
        bess_document = _load(bess_model)
        bess_document["simulation"]["parameters"]["initial_soc"] = bess_initial_soc
        _dump(bess_model, bess_document)

    harness.tokens = {name: os.urandom(24).hex() for name in TOKEN_ENVS}
    cli.auth_tokens.update(harness.tokens)
    pki = write_mtls_pki(plant / "collector-pki", ("cli",))
    harness.pki = pki

    gateway_service = _write_gateway_service(plant, grpc_port=ports["gateway"])
    gateway_document = _load(gateway_service)
    gateway_document["grpc"]["sink_listener_port"] = ports["gateway_sink"]
    servers = gateway_document["servers"]
    assert [item["device_id"] for item in servers] == list(SIM_DEVICES)
    servers.append(
        {
            "server_id": "gw-gw02",
            "device_id": REPLAY_DEVICE,
            "listen_host": "127.0.0.1",
            "listen_port": harness.modbus_ports[REPLAY_DEVICE],
            "desired_state": "START",
            "control": {"enabled": False, "target": {"kind": "none"}, "mappings": []},
        }
    )
    for server in servers:
        server["server_id"] = f"gw-{server['device_id'].lower()}"
        server["listen_host"] = "127.0.0.1"
        server["listen_port"] = harness.modbus_ports[server["device_id"]]
    ppc = servers[4]
    assert ppc["server_id"] == "gw-ppc_sim01"
    ppc["control"]["mappings"] = [
        {"variable_id": point_id, "target_point_id": point_id}
        for point_id in (
            "agc_enable",
            "agc_power_setpoint",
            "avc_enable",
            "avc_voltage_setpoint",
            "avc_reactive_setpoint",
            "ffr_enable",
        )
    ]
    ppc["control"]["target"]["endpoint"] = harness.endpoints["commander"]
    ppc["control"]["target"]["token_env"] = GATEWAY_COMMANDER_TOKEN_ENV
    ppc["control"]["simulation"] = {
        "endpoint": harness.endpoints["data-generator"],
        "token_env": GATEWAY_GENERATOR_TOKEN_ENV,
        "scenario_id": "wind-plant-physical",
    }
    _dump(gateway_service, gateway_document)

    dg_service = _write_dg_service(
        plant,
        bind=harness.endpoints["data-generator"],
        scenarios=_build_scenarios(harness.gw_grpc_endpoint),
    )

    collector_services = {
        "collector_realtime": _write_collector_service(
            plant, "collector-full-chain-rt", nats_url=stack.nats, delivery="realtime"
        ),
        "collector_reliable": _write_collector_service(
            plant,
            "collector-full-chain-reliable",
            nats_url=stack.nats,
            delivery="reliable",
        ),
    }

    # ---------------- commander ----------------
    audit_stream = f"CMD_FULLCHAIN_{harness.token.upper()}"
    audit_subject = f"commander.fullchain.{harness.token}"
    asyncio.run(_provision_commander_audit(stack.nats, audit_stream, audit_subject))
    commander_service = _write_commander_service(
        plant,
        grpc_port=ports["commander"],
        gateway_endpoint=harness.gw_grpc_endpoint,
        nats_url=stack.nats,
        audit_stream=audit_stream,
        audit_subject=audit_subject,
    )
    _write_deployment(plant)

    # ---------------- sink ----------------
    sink = SinkSystemHarness(
        root=root / "sink",
        stack=stack,
        grpc_port=ports["sink"],
        namespace=f"fullchain-{harness.token}",
        kafka_topic=f"fullchain-{harness.token}",
        history_stream=f"WH_HISTORY_FC_{harness.token.upper()}",
        dlq_stream=f"WH_DLQ_FC_{harness.token.upper()}",
        dlq_subject=f"wh.fullchain.dlq.{harness.token}",
        token=harness.token,
    )
    harness.sink = sink
    asyncio.run(_provision_kafka_topic(stack.kafka, sink.kafka_topic))
    sink.write_dual_channel_root(
        kafka_bootstrap=stack.kafka, redis_address=stack.redis, nats_url=stack.nats
    )
    sink_service = sink.root / SINK_SERVICE_FILE
    sink_service_document = _load(sink_service)
    sink_service_document["wind_farm_id"] = FARM
    _dump(sink_service, sink_service_document)
    cli.sink_token = sink.auth_tokens["admin"]
    bounded_rt = {
        "wind_farm_id": FARM,
        "collector_id": harness.rt_collector,
        "devices": [],
        "points": [],
    }
    bounded_hist = {
        "wind_farm_id": FARM,
        "collector_id": harness.reliable_collector,
        "devices": [],
        "points": [],
    }
    bounded_gateway = {
        "wind_farm_id": FARM,
        "collector_id": harness.rt_collector,
        "devices": ["WTG_SIM01"],
        "points": ["wind_speed"],
    }
    deliveries = _load(sink.root / DELIVERIES_INSTANCE)
    deliveries["deliveries"]["ems_power"]["source"] = bounded_rt
    deliveries["deliveries"]["power_history"]["source"] = bounded_hist
    deliveries["deliveries"]["gateway_output"] = {
        "template_id": "realtime_power",
        "template_version": "1.0.0",
        "sink_id": "gateway_sink",
        "desired_state": "START",
        "source": bounded_gateway,
        "target_mappings": [
            {
                "source_device_id": "WTG_SIM01",
                "source_point_id": "wind_speed",
                "variable_id": SINK_TARGET_POINT,
            }
        ],
        "overrides": {},
    }
    deliveries["deliveries"]["modbus_output"] = {
        "template_id": "realtime_power",
        "template_version": "1.0.0",
        "sink_id": "modbus_sink",
        "desired_state": "START",
        "source": bounded_gateway,
        "overrides": {},
    }
    for device_id, point_id in GOLDEN_DEVICES.items():
        suffix = device_id.lower()
        bounded_source = {
            "wind_farm_id": FARM,
            "collector_id": harness.rt_collector,
            "devices": [device_id],
            "points": [point_id],
        }
        deliveries["deliveries"][f"gateway_output_{suffix}"] = {
            "template_id": "realtime_power",
            "template_version": "1.0.0",
            "sink_id": f"gateway_sink_{suffix}",
            "desired_state": "START",
            "source": bounded_source,
            "target_mappings": [
                {
                    "source_device_id": device_id,
                    "source_point_id": point_id,
                    "variable_id": SINK_TARGET_POINT,
                }
            ],
            "overrides": {},
        }
        deliveries["deliveries"][f"modbus_output_{suffix}"] = {
            "template_id": "realtime_power",
            "template_version": "1.0.0",
            "sink_id": f"modbus_sink_{suffix}",
            "desired_state": "START",
            "source": bounded_source,
            "overrides": {},
        }
    _dump(sink.root / DELIVERIES_INSTANCE, deliveries)

    sinks_doc = _load(sink.root / SINKS_INSTANCE)
    sinks_doc["sinks"]["gateway_sink"] = {
        "template_id": "gateway_realtime",
        "template_version": "1.1.0",
        "connection": {
            "endpoint": f"127.0.0.1:{ports['gateway_sink']}",
            "gateway_id": "gw-wtg_sim01",
        },
        "overrides": {},
    }
    sinks_doc["sinks"]["modbus_sink"] = {
        "template_id": "modbus_registers",
        "template_version": "1.0.0",
        "connection": {"listen_host": "127.0.0.1", "port": harness.sink_modbus_port},
        "overrides": {},
    }
    for device_id in GOLDEN_DEVICES:
        suffix = device_id.lower()
        sinks_doc["sinks"][f"gateway_sink_{suffix}"] = {
            "template_id": "gateway_realtime",
            "template_version": "1.1.0",
            "connection": {
                "endpoint": f"127.0.0.1:{ports['gateway_sink']}",
                "gateway_id": f"gw-{suffix}",
            },
            "overrides": {},
        }
        sinks_doc["sinks"][f"modbus_sink_{suffix}"] = {
            "template_id": "modbus_registers",
            "template_version": "1.0.0",
            "connection": {
                "listen_host": "127.0.0.1",
                "port": harness.sink_modbus_ports[device_id],
            },
            "overrides": {},
        }
    _dump(sink.root / SINKS_INSTANCE, sinks_doc)
    for template_name in ("gateway_realtime", "modbus_registers"):
        version = "1.1.0" if template_name == "gateway_realtime" else "1.0.0"
        source = REPO_CONFIGS / "templates" / "sinks" / f"sink_{template_name}_v{version}.yaml"
        _dump(
            sink.root / "configs" / "templates" / "sinks" / f"sink_{template_name}_v{version}.yaml",
            _load(source),
        )
    harness.record(
        "STACK-CFG",
        config_root=str(plant),
        sink_root=str(sink.root),
        modbus_ports=harness.modbus_ports,
        ports=ports,
    )

    # ---------------- CLI-managed native processes ----------------
    python = sys.executable
    common_env = {
        "WIND_HUB_COLLECTOR_TLS_CERT": str(pki["server_crt"]),
        "WIND_HUB_COLLECTOR_TLS_KEY": str(pki["server_key"]),
        "WIND_HUB_COLLECTOR_TLS_CA": str(pki["ca"]),
        "WIND_HUB_COLLECTOR_ADMIN_TOKEN": ADMIN_TOKEN,
    }
    argv_by_service: dict[str, tuple[list[str], Path, dict[str, str]]] = {
        "gateway": (
            [
                python,
                "-m",
                "industrial_gateway.main",
                "--config",
                str(gateway_service),
                "--project-root",
                str(plant),
            ],
            plant,
            {
                "WIND_HUB_GATEWAY_SINK_TOKEN": gateway_sink_token,
                **{
                    name: harness.tokens[name]
                    for name in (
                        GATEWAY_CLI_TOKEN_ENV,
                        GENERATOR_GATEWAY_TOKEN_ENV,
                        COMMANDER_GATEWAY_TOKEN_ENV,
                        GATEWAY_GENERATOR_TOKEN_ENV,
                        GATEWAY_COMMANDER_TOKEN_ENV,
                    )
                },
            },
        ),
        "data-generator": (
            [python, "-m", "data_generator.main", "--config", str(dg_service)],
            plant,
            {
                name: harness.tokens[name]
                for name in (
                    GENERATOR_CLI_TOKEN_ENV,
                    GATEWAY_GENERATOR_TOKEN_ENV,
                    GENERATOR_GATEWAY_TOKEN_ENV,
                )
            },
        ),
        "collector_realtime": (
            [
                python,
                "-m",
                "collector.main",
                "--config",
                str(collector_services["collector_realtime"]),
                "--state-directory",
                str(root / "collector-rt" / "state"),
                "--collector-id",
                harness.rt_collector,
                "--grpc-host",
                "127.0.0.1",
                "--grpc-port",
                str(ports["collector_realtime"]),
                "--shutdown-timeout",
                "10",
            ],
            SRC,
            common_env,
        ),
        "collector_reliable": (
            [
                python,
                "-m",
                "collector.main",
                "--config",
                str(collector_services["collector_reliable"]),
                "--state-directory",
                str(root / "collector-hist" / "state"),
                "--collector-id",
                harness.reliable_collector,
                "--grpc-host",
                "127.0.0.1",
                "--grpc-port",
                str(ports["collector_reliable"]),
                "--shutdown-timeout",
                "10",
            ],
            SRC,
            common_env,
        ),
        "sink": (
            [
                python,
                "-m",
                "sink_service.main",
                "--config-root",
                str(sink.root),
                "--service-file",
                SINK_SERVICE_FILE,
            ],
            sink.root,
            {
                "WIND_HUB_SINK_CLI_TOKEN": sink.auth_tokens["admin"],
                "WIND_HUB_GATEWAY_SINK_TOKEN": gateway_sink_token,
            },
        ),
        "commander": (
            [python, "-m", "commander", "--config", str(commander_service)],
            SRC,
            {
                name: harness.tokens[name]
                for name in (
                    COMMANDER_OPS_TOKEN_ENV,
                    COMMANDER_MAINTENANCE_TOKEN_ENV,
                    GATEWAY_COMMANDER_TOKEN_ENV,
                    COMMANDER_GATEWAY_TOKEN_ENV,
                )
            },
        ),
    }
    for name in SERVICES:
        argv, cwd, env = argv_by_service[name]
        cli.register_process(name, argv, cwd=cwd, env=env)
    _write_cli(
        harness.cli_rt,
        collector="collector_realtime",
        endpoints=harness.endpoints,
        processes=cli.processes,
        collector_pki=pki,
    )
    _write_cli(
        harness.cli_rel,
        collector="collector_reliable",
        endpoints=harness.endpoints,
        processes=cli.processes,
        collector_pki=pki,
    )

    try:
        asyncio.run(_wait_stack_ready(harness, sink))
    except BaseException:
        _teardown(harness)
        raise
    return harness


async def _up_stack_async() -> r5_stack.R5Stack:
    return await asyncio.to_thread(r5_stack.up_stack)


async def _provision_kafka_topic(bootstrap: str, topic: str) -> None:
    """Create the isolated reliable-output Topic before Sink workers start."""
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from aiokafka.errors import TopicAlreadyExistsError

    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        with contextlib.suppress(TopicAlreadyExistsError):
            await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    finally:
        await admin.close()


async def _wait_stack_ready(harness: FullChainHarness, sink: SinkSystemHarness) -> None:
    await sink.provision_streams()
    for name in SERVICES:
        await harness.cli.service(harness.cli_rt, "start", name)
    await harness.poll(
        harness.cli_rt,
        "gateway",
        "list",
        predicate=lambda data: bool(data.get("gateways")),
        timeout=75,
    )
    harness.record("STACK-UP", services=list(SERVICES))


def _teardown(harness: FullChainHarness) -> None:
    asyncio.run(_teardown_async(harness))


async def _teardown_async(harness: FullChainHarness) -> None:
    cli = harness.cli
    if harness.generation_id is not None and harness.cli_rt.exists():
        await cli.cli(
            harness.cli_rt,
            "data-generator",
            "stop",
            harness.generation_id,
            "--yes",
            timeout=15,
            check=False,
        )
        harness.generation_id = None
    if harness.cli_rt.exists():
        await cli.stop_all(harness.cli_rt)
    if harness.sink is not None:
        await harness.sink.delete_streams()
    await asyncio.to_thread(r5_stack.down_stack, harness.stack)


@pytest.fixture(scope="session")
def full_chain(tmp_path_factory: pytest.TempPathFactory) -> FullChainHarness:
    """Session 级现场（同步装配、异步操作在各自测试事件循环内执行）。"""
    tmp_path = tmp_path_factory.mktemp("full-chain")
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable for real middleware stack")
    harness = _build_and_start(tmp_path)
    try:
        yield harness
    finally:
        _teardown(harness)


@pytest.fixture
def multirate_chain(tmp_path_factory: pytest.TempPathFactory) -> FullChainHarness:
    """Isolated full chain with four independently configured task periods."""
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable for real middleware stack")
    harness = _build_and_start(
        tmp_path_factory.mktemp("multirate-chain"),
        {"WTG_SIM01": 0.05, "PV_SIM01": 0.1, "BESS_SIM01": 0.2, "GRID_SIM01": 1.0},
    )
    try:
        yield harness
    finally:
        _teardown(harness)


@pytest.fixture(params=[("upper", 0.8999, -500000.0), ("lower", 0.1001, 500000.0)])
def bess_boundary_chain(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> FullChainHarness:
    """Isolated full chain with BESS initial SOC adjacent to one physical bound."""
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable for real middleware stack")
    boundary, initial_soc, power = request.param
    harness = _build_and_start(
        tmp_path_factory.mktemp(f"bess-{boundary}-chain"),
        bess_initial_soc=initial_soc,
    )
    harness.bess_boundary = boundary
    harness.bess_boundary_power = power
    try:
        yield harness
    finally:
        _teardown(harness)


# ----------------------------- 独立读回原语 -----------------------------


async def read_gateway_modbus_float(port: int, address: int) -> float:
    """独立 pymodbus 客户端读取 Gateway 下行 input 寄存器并解码 float32。"""
    from pymodbus.client import AsyncModbusTcpClient

    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    await client.connect()
    try:
        response = await client.read_input_registers(address, count=2, device_id=1)
        if response.isError():
            raise RuntimeError(f"gateway modbus read failed: {response}")
        return struct.unpack(">f", struct.pack(">HH", *response.registers))[0]
    finally:
        client.close()


async def wait_gateway_modbus_float(
    port: int, address: int, predicate: Callable[[float], bool], timeout: float = 60
) -> float:
    deadline = time.monotonic() + timeout
    latest: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = await read_gateway_modbus_float(port, address)
            if predicate(value):
                return value
        except (OSError, RuntimeError) as error:
            latest = error
        await asyncio.sleep(0.4)
    raise AssertionError(f"gateway modbus value not observed on port {port}: {latest}")


async def wait_sink_modbus_float(
    port: int, expected: float, timeout: float = 60, tolerance: float = 1e-3
) -> tuple[float, int]:
    """Read Sink's own Modbus output server with an independent client."""
    from pymodbus.client import AsyncModbusTcpClient

    deadline = time.monotonic() + timeout
    latest: Exception | None = None
    while time.monotonic() < deadline:
        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        try:
            await client.connect()
            values = await client.read_holding_registers(1000, count=2, device_id=1)
            quality = await client.read_holding_registers(2000, count=1, device_id=1)
            if values.isError() or quality.isError():
                raise RuntimeError(f"sink modbus read failed: {values!r}, {quality!r}")
            value = struct.unpack(">f", struct.pack(">HH", *values.registers))[0]
            code = int(quality.registers[0])
            if abs(value - expected) <= tolerance and code == 0:
                return value, code
        except (OSError, RuntimeError) as error:
            latest = error
        finally:
            client.close()
        await asyncio.sleep(0.4)
    raise AssertionError(f"sink modbus output not observed on port {port}: {latest}")
