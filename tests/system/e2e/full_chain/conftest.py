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

设备清单双轨：DG 使用出厂 ``devices_sim.yaml`` 与原始物理点表；
Gateway/Collector/Commander 使用派生的 ``devices_e2e.yaml`` 与 E2E 型号。
其中 WTG E2E 点表单独追加 Sink 回写目标，物理输出点仍沿用出厂地址，
避免将测试回写点误作为 DG 必须生成的物理量。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import struct
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
from tests.commander.grpc_harness import build_pki
from tests.fixtures.services import r5_stack
from tests.sink_service._stage1_fixtures import DELIVERIES_INSTANCE, SINKS_INSTANCE
from tests.sink_service._stage1_tls import write_mtls_bundle
from tests.sink_service.system.conftest import SinkSystemHarness
from tests.support.collector_config_tree import _dump
from tests.support.commander_mtls import write_commander_service
from tests.support.gateway import reserve_port, write_mtls_pki

SRC = Path(__file__).resolve().parents[4] / "src"
REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_CONFIGS = REPO_ROOT / "configs"

#: 出厂 site.yaml 的站点身份——NATS 批次与 Sink source 过滤器必须与此一致。
FARM = "example_site"
ADMIN_TOKEN = ADMIN_TOKEN
SIM_DEVICES = ("WTG_SIM01", "PV_SIM01", "BESS_SIM01", "GRID_SIM01", "PPC_SIM01")

#: Sink GatewayOutput 的专用目标变量（寄存器随出厂 sim WTG 点表副本追加，
#: 不与 DG 注入变量冲突）。
SINK_TARGET_POINT = "e2e_sink_target"


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path}: expected mapping"
    return document


def _factory_variable_mappings() -> dict[str, dict[str, str]]:
    """Load all source-to-Gateway point identities from the shipped scenario."""
    shipped = _load(REPO_CONFIGS / "services" / "data-generator-sim.example.yaml")
    scenarios = shipped.get("scenarios")
    assert isinstance(scenarios, list) and scenarios, "factory simulation scenario is missing"
    routes = scenarios[0].get("gateways")
    assert isinstance(routes, list), "factory Gateway routes are missing"

    devices = _load(REPO_CONFIGS / "instances" / "devices_sim.yaml")["devices"]
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
    """devices_sim 副本：endpoint 指向本用例 Gateway 下行 Modbus 监听端口。"""
    document = _load(REPO_CONFIGS / "instances" / "devices_sim.yaml")
    for device_id, port in modbus_ports.items():
        document["devices"][device_id]["endpoint"]["port"] = port
    _dump(root / "instances" / "devices_sim.yaml", document)


def _write_e2e_devices(root: Path) -> None:
    """Collector/Commander 设备清单：同端点、e2e 型号（带任务引用）。"""
    document = _load(root / "instances" / "devices_sim.yaml")
    devices = {}
    for device_id in SIM_DEVICES:
        source = document["devices"][device_id]
        devices[device_id] = {
            "name": source["name"],
            "enabled": source["enabled"],
            "model": {"id": f"e2e_{device_id.lower()}", "version": "1.0.0"},
            "endpoint": {
                "host": "127.0.0.1",
                "port": int(source["endpoint"]["port"]),
            },
            "overrides": {},
        }
    _dump(
        root / "instances" / "devices_e2e.yaml",
        {"schema_version": 1, "file_version": "1.0.0", "devices": devices},
    )


def _write_e2e_models_and_tasks(root: Path, intervals: dict[str, float] | None = None) -> None:
    """e2e 设备型号（sim 物理型号 + 任务引用）与逐设备任务模板。"""
    model_source = {
        "WTG_SIM01": "wtg_sim_physical",
        "PV_SIM01": "pv_sim_physical",
        "BESS_SIM01": "bess_sim_physical",
        "GRID_SIM01": "grid_sim_physical",
        "PPC_SIM01": "plant_controller_sim_physical",
    }
    for device_id, sim_model in model_source.items():
        document = _load(
            REPO_CONFIGS / "templates" / "device_models" / f"device_model_{sim_model}_v1.0.0.yaml"
        )
        document["template_id"] = f"e2e_{device_id.lower()}"
        if device_id == "WTG_SIM01":
            # Sink's independent write-back point is not a physical DG output.
            # Only the Gateway/Collector E2E model uses the extended point table.
            document["point_table"]["id"] = "modbus_wtg_sim_e2e"
        document["task"] = {"id": f"e2e_task_{device_id.lower()}", "version": "1.0.0"}
        _dump(
            root
            / "templates"
            / "device_models"
            / f"device_model_e2e_{device_id.lower()}_v1.0.0.yaml",
            document,
        )
        _dump(
            root / "templates" / "tasks" / f"task_e2e_task_{device_id.lower()}_v1.0.0.yaml",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "template_id": f"e2e_task_{device_id.lower()}",
                "point_group": "all",
                "interval": (intervals or {}).get(device_id, 0.2),
                "desired_state": "STOP",
            },
        )
    # Sink GatewayOutput 专用目标变量：寄存器追加进出厂 sim WTG 点表副本
    # （Gateway 与 Collector 共享同一根下同一份点表，寄存器布局天然一致）。
    point_table = (
        REPO_CONFIGS / "templates" / "point_tables" / "point_table_modbus_wtg_sim_v1.0.0.yaml"
    )
    document = _load(point_table)
    document["template_id"] = "modbus_wtg_sim_e2e"
    document["points"][SINK_TARGET_POINT] = {
        "business_point_id": SINK_TARGET_POINT,
        "point_groups": ["all"],
        "address": {"register_type": "holding", "address": 400, "count": 2, "slave_id": 1},
        "data_type": "float32",
        "unit": "none",
        "scale": 1.0,
        "offset": 0.0,
        "description": "E2E Sink GatewayOutput target",
    }
    business = _load(root / "templates" / "business_points_standard.yaml")
    business["business_points"][SINK_TARGET_POINT] = {"data_type": "float32", "unit": "none"}
    _dump(root / "templates" / "business_points_standard.yaml", business)
    _dump(
        root / "templates" / "point_tables" / "point_table_modbus_wtg_sim_e2e_v1.0.0.yaml",
        document,
    )


def _write_collector_service(root: Path, filename: str, *, nats_url: str, delivery: str) -> Path:
    document = _load(REPO_CONFIGS / "services" / "collector.yaml")
    document["config_sources"] = {
        "instances": {
            "site": "instances/site.yaml",
            "devices": "instances/devices_e2e.yaml",
        },
        "templates": {"root": "templates"},
    }
    document["messaging"]["servers"] = [nats_url]
    document["messaging"]["delivery"] = delivery
    document["runtime"].update(
        {"connect_timeout": 5.0, "read_timeout": 5.0, "shutdown_timeout": 10.0}
    )
    document["watchdog"] = {"interval": 0.5, "health_timeout": 0.5}
    path = root / "services" / filename
    _dump(path, document)
    return path


def _write_gateway_service(root: Path, *, grpc_port: int, pki: dict[str, Any]) -> Path:
    document = _load(REPO_CONFIGS / "services" / "industrial_gateway-sim.example.yaml")
    document["config_sources"] = {
        "instances": {"devices": "instances/devices_e2e.yaml"},
        "templates": {"root": "templates"},
    }
    document["grpc"]["listen_port"] = grpc_port
    document["grpc"]["tls"] = {
        "enabled": True,
        "ca_file": str(pki["ca"]),
        "cert_file": str(pki["server_crt"]),
        "key_file": str(pki["server_key"]),
    }
    document["grpc"]["permissions"] = {
        "cli": ["read", "lifecycle", "inject", "diagnose"],
        "generator": ["inject", "read"],
        "commander": ["simulation_execute", "read"],
        "sink": ["inject", "read"],
    }
    path = root / "services" / "industrial_gateway.yaml"
    _dump(path, document)
    return path


def _write_dg_service(
    root: Path,
    *,
    bind: str,
    pki: dict[str, Any],
    scenarios: list[dict[str, Any]],
) -> Path:
    document = _load(REPO_CONFIGS / "services" / "data-generator-sim.example.yaml")
    document["config_sources"] = {
        "instances": {"site": "instances/site.yaml", "devices": "instances/devices_sim.yaml"},
        "templates": {"root": "templates"},
    }
    document["service"]["grpc_bind"] = bind
    clients = pki["clients"]
    generator_crt, generator_key = clients["generator"]
    document["output"]["gateway_tls"] = {
        "ca_file": str(pki["ca"]),
        "cert_file": str(generator_crt),
        "key_file": str(generator_key),
    }
    document["security"] = {
        "tls": {
            "ca_file": str(pki["ca"]),
            "cert_file": str(pki["server_crt"]),
            "key_file": str(pki["server_key"]),
        },
        "permissions": {
            "cli": ["variables_read", "variables_update"],
            "gateway": ["variables_read", "variables_update"],
        },
    }
    document["recording"] = {"enabled": True, "directory": "recording"}
    document["scenarios"] = scenarios
    path = root / "services" / "data-generator.yaml"
    _dump(path, document)
    return path


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
        self.cli_rt: Path = root / "cli-realtime.yaml"
        self.cli_rel: Path = root / "cli-reliable.yaml"
        self.sink: SinkSystemHarness | None = None
        self.pki: dict[str, Any] = {}
        self.plant: Path = root / "plant-configs"
        # Data Generator resolves recording.directory against its service root,
        # which is the Full-chain temporary root rather than plant-configs.
        self.recording_dir = root / "recording"
        self.generation_id: str | None = None
        self.evidence_path = root / "evidence.jsonl"
        self.sink_modbus_port = 0

    # ---------------------------- evidence ----------------------------
    def record(self, case: str, **payload: Any) -> None:
        entry = {"case_id": case, "time": time.time(), **payload}
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
        _physical_scenario(
            "e2e-wind-seeded",
            "1.0.0",
            gateway_endpoint,
            interval=0.05,
            weather=_weather_block(turbulence=0.12),
        ),
    ]
    # PV 短日场景：辐照 0→800（cloud 0.8）与 0→1000（cloud 1.0）。
    for scenario_id, cloud in (("e2e-pv-800", 0.8), ("e2e-pv-1000", 1.0)):
        document = _physical_scenario(scenario_id, "1.0.0", gateway_endpoint, interval=0.05)
        irradiance = document["weather"]["site-a"]["irradiance"]
        irradiance.update({"sunrise_s": 0, "sunset_s": 40, "cloud_factor_target": cloud})
        scenarios.append(document)
    return scenarios


def _build_and_start(root: Path, intervals: dict[str, float] | None = None) -> FullChainHarness:
    stack = asyncio.run(_up_stack_async())
    cli = StrictCliE2E(root)
    harness = FullChainHarness(root, stack, cli)
    ports = {name: reserve_port() for name in SERVICES}
    harness.ports = ports
    harness.endpoints = {name: f"127.0.0.1:{port}" for name, port in ports.items()}
    harness.modbus_ports = {device_id: reserve_port() for device_id in SIM_DEVICES}
    harness.gw_grpc_endpoint = harness.endpoints["gateway"]
    harness.sink_modbus_port = reserve_port()

    plant = _plant_root(root)
    harness.plant = plant
    _write_sim_devices(plant, harness.modbus_ports)
    _write_e2e_models_and_tasks(plant, intervals)
    _write_e2e_devices(plant)

    pki = write_mtls_pki(
        plant / "gateway-pki", ("cli", "gateway", "generator", "sink", "commander")
    )
    harness.pki = pki
    clients = pki["clients"]

    gateway_service = _write_gateway_service(plant, grpc_port=ports["gateway"], pki=pki)
    gateway_document = _load(gateway_service)
    servers = gateway_document["servers"]
    assert [item["device_id"] for item in servers] == list(SIM_DEVICES)
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
    ppc["control"]["target"]["tls"] = {
        "enabled": True,
        "ca_file": str(plant / "commander-pki" / "ca.crt"),
        "cert_file": str(pki["server_crt"]),
        "key_file": str(pki["server_key"]),
    }
    gateway_crt, gateway_key = clients["gateway"]
    ppc["control"]["simulation"] = {
        "endpoint": harness.endpoints["data-generator"],
        "tls": {
            "enabled": True,
            "ca_file": str(pki["ca"]),
            "cert_file": str(gateway_crt),
            "key_file": str(gateway_key),
        },
        "scenario_id": "wind-plant-physical",
    }
    _dump(gateway_service, gateway_document)

    dg_service = _write_dg_service(
        plant,
        bind=harness.endpoints["data-generator"],
        pki=pki,
        scenarios=_build_scenarios(harness.gw_grpc_endpoint),
    )

    collector_services = {
        "collector_realtime": _write_collector_service(
            plant, "collector-rt.yaml", nats_url=stack.nats, delivery="realtime"
        ),
        "collector_reliable": _write_collector_service(
            plant, "collector-hist.yaml", nats_url=stack.nats, delivery="reliable"
        ),
    }

    # ---------------- commander ----------------
    audit_stream = f"CMD_FULLCHAIN_{harness.token.upper()}"
    audit_subject = f"commander.fullchain.{harness.token}"
    asyncio.run(_provision_commander_audit(stack.nats, audit_stream, audit_subject))
    (plant / "commander-pki").mkdir(parents=True, exist_ok=True)
    commander_pki = build_pki(plant / "commander-pki")
    commander_service = write_commander_service(
        plant, plant, commander_pki, grpc_port=ports["commander"]
    )
    document = _load(commander_service)
    document["config_sources"]["instances"]["devices"] = str(
        plant / "instances" / "devices_e2e.yaml"
    )
    document["audit"]["nats_servers"] = [stack.nats]
    document["audit"]["stream"] = audit_stream
    document["audit"]["subject"] = audit_subject
    document["simulation_targets"] = {
        "PPC_SIM01": {
            "gateway_id": "gw-ppc_sim01",
            "endpoint": harness.gw_grpc_endpoint,
            "ca_cert": str(pki["ca"]),
            "client_cert": str(clients["commander"][0]),
            "client_key": str(clients["commander"][1]),
        }
    }
    _dump(commander_service, document)

    # ---------------- sink ----------------
    bundle = write_mtls_bundle(root / "sink-pki")
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
        bundle=bundle,
    )
    harness.sink = sink
    asyncio.run(_provision_kafka_topic(stack.kafka, sink.kafka_topic))
    sink.write_dual_channel_root(
        kafka_bootstrap=stack.kafka, redis_address=stack.redis, nats_url=stack.nats
    )
    sink_service = sink.root / "configs" / "services" / "sink_service.yaml"
    sink_service_document = _load(sink_service)
    sink_service_document["wind_farm_id"] = FARM
    _dump(sink_service, sink_service_document)
    sink.bundle = write_mtls_bundle(sink.root)
    bundle = sink.bundle
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
    _dump(sink.root / DELIVERIES_INSTANCE, deliveries)

    sinks_doc = _load(sink.root / SINKS_INSTANCE)
    sinks_doc["sinks"]["gateway_sink"] = {
        "template_id": "gateway_realtime",
        "template_version": "1.0.0",
        "connection": {
            "endpoint": harness.gw_grpc_endpoint,
            "gateway_id": "gw-wtg_sim01",
            "credential_ref": "env:WH_FULLCHAIN_GATEWAY_TLS",
        },
        "overrides": {},
    }
    sinks_doc["sinks"]["modbus_sink"] = {
        "template_id": "modbus_registers",
        "template_version": "1.0.0",
        "connection": {"listen_host": "127.0.0.1", "port": harness.sink_modbus_port},
        "overrides": {},
    }
    _dump(sink.root / SINKS_INSTANCE, sinks_doc)
    for template_name in ("gateway_realtime", "modbus_registers"):
        source = REPO_CONFIGS / "templates" / "sinks" / f"sink_{template_name}_v1.0.0.yaml"
        _dump(
            sink.root / "configs" / "templates" / "sinks" / f"sink_{template_name}_v1.0.0.yaml",
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
            {},
        ),
        "data-generator": (
            [python, "-m", "data_generator.main", "--config", str(dg_service)],
            plant,
            {},
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
            [python, "-m", "sink_service.main", "--config-root", str(sink.root)],
            sink.root,
            {
                "WH_FULLCHAIN_GATEWAY_TLS": json.dumps(
                    {
                        "ca_pem": Path(pki["ca"]).read_text(encoding="utf-8"),
                        "client_cert_pem": clients["sink"][0].read_text(encoding="utf-8"),
                        "client_key_pem": clients["sink"][1].read_text(encoding="utf-8"),
                    }
                )
            },
        ),
        "commander": (
            [python, "-m", "commander", "--config", str(commander_service)],
            SRC,
            {},
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
        gateway_pki=pki,
        commander_pki=commander_pki,
        sink_bundle=bundle,
    )
    _write_cli(
        harness.cli_rel,
        collector="collector_reliable",
        endpoints=harness.endpoints,
        processes=cli.processes,
        gateway_pki=pki,
        commander_pki=commander_pki,
        sink_bundle=bundle,
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
    port: int, expected: float, timeout: float = 60
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
            if abs(value - expected) < 1e-3 and code == 0:
                return value, code
        except (OSError, RuntimeError) as error:
            latest = error
        finally:
            client.close()
        await asyncio.sleep(0.4)
    raise AssertionError(f"sink modbus output not observed on port {port}: {latest}")
