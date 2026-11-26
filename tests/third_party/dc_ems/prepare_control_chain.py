"""生成独立的 Modbus → Gateway1 → Commander → Gateway2 → DG 控制配置。"""

from __future__ import annotations

import argparse
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import yaml

from tests.third_party.dc_ems.stage_config import ROOT

VERSION = "1.0.0"
DEPLOYMENT = "dcems-control"
SOURCE = ROOT / "deploy" / "config_roots" / "e2e-final"
INGRESS_PORT = 15431
EXECUTION_GRPC = 52173
INGRESS_GRPC = 52174
COMMANDER_GRPC = 52152
GENERATOR_GRPC = 52161


def _read(path: Path) -> dict[str, Any]:
    """读取已发布的配置样板。"""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _write(path: Path, document: dict[str, Any]) -> None:
    """写入新的隔离配置文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def prepare(root: Path, *, nats_url: str = "nats://127.0.0.1:14223") -> Path:
    """生成不可覆盖的专用根，不改动既有测量或 E2E 发布配置。"""
    root = root.resolve()
    if root.exists():
        raise FileExistsError(root)
    if root == ROOT or ROOT in root.parents and root.parent.name != "run":
        raise ValueError("control root must be a new dedicated runtime directory")
    if not SOURCE.is_dir():
        raise FileNotFoundError(SOURCE)
    config = root / "configs"
    shutil.copytree(SOURCE, config)
    for directory in ("services", "deployments"):
        for path in (config / directory).glob("*.yaml"):
            path.unlink()

    # 物理模型仍引用已冻结的五设备 Scenario；仅移动仿真 Gateway 地址，
    # 不能将原 dcems-random-ten 的随机模型冒充为可控制的 PPC。
    scenarios = _read(SOURCE / "instances" / "scenarios_e2e-final_v1.0.0.yaml")
    for scenario in scenarios["scenarios"]:
        if scenario["scenario_id"] == "e2e-plant-physical":
            for gateway in scenario["gateways"]:
                gateway["endpoint"] = f"127.0.0.1:{EXECUTION_GRPC}"
    _write(config / "instances" / "scenarios_e2e-final_v1.0.0.yaml", scenarios)

    devices = _read(SOURCE / "instances" / "device_e2e-final_v1.0.1.yaml")
    for index, device_id in enumerate(("WTG_SIM01", "PV_SIM01", "BESS_SIM01", "GRID_SIM01", "PPC_SIM01")):
        devices["devices"][device_id]["endpoint"]["port"] = 15421 + index
    _write(config / "instances" / "device_e2e-final_v1.0.1.yaml", devices)

    execution = _read(SOURCE / "services" / "service_gateway-e2e-source_v1.1.3.yaml")
    execution["service_id"] = "gateway-dcems-control-execution"
    execution["file_version"] = VERSION
    execution["grpc"]["listen_port"] = EXECUTION_GRPC
    for index, server in enumerate(execution["servers"]):
        server["listen_port"] = 15421 + index
    ppc = next(server for server in execution["servers"] if server["device_id"] == "PPC_SIM01")
    # Gateway2 接受来自 Commander 的仿真执行 RPC，按白名单控制点转发到 DG。
    ppc["control"]["target"]["endpoint"] = f"127.0.0.1:{COMMANDER_GRPC}"
    ppc["control"]["simulation"]["endpoint"] = f"127.0.0.1:{GENERATOR_GRPC}"
    ppc["control"]["mappings"] = [{"variable_id": "agc_enable", "target_point_id": "agc_enable"}]
    _write(config / "services" / "service_gateway-dcems-control-execution_v1.0.0.yaml", execution)

    ingress = deepcopy(execution)
    ingress["service_id"] = "gateway-dcems-control-ingress"
    ingress["grpc"]["listen_port"] = INGRESS_GRPC
    ingress["servers"] = [deepcopy(ppc)]
    incoming = ingress["servers"][0]
    incoming["server_id"] = "dcems-control-ingress-ppc"
    incoming["listen_port"] = INGRESS_PORT
    incoming["control"].pop("simulation")
    incoming["control"]["target"]["endpoint"] = f"127.0.0.1:{COMMANDER_GRPC}"
    # Gateway1 仅发起受控转发，不具有仿真执行或数据注入权限。
    ingress["security"]["callers"] = [
        {"caller_id": "operator", "token_env": "WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN",
         "permissions": ["read", "lifecycle", "diagnose"]}
    ]
    _write(config / "services" / "service_gateway-dcems-control-ingress_v1.0.0.yaml", ingress)

    commander = _read(SOURCE / "services" / "service_commander-e2e_v1.1.0.yaml")
    commander["service_id"] = "commander-dcems-control"
    commander["file_version"] = VERSION
    commander["grpc"]["listen_port"] = COMMANDER_GRPC
    commander["simulation_targets"]["PPC_SIM01"]["endpoint"] = f"127.0.0.1:{EXECUTION_GRPC}"
    commander["repository"]["sqlite_path"] = str(root / "state" / "commander" / "events.db")
    commander["audit"]["nats_servers"] = [nats_url]
    _write(config / "services" / "service_commander-dcems-control_v1.0.0.yaml", commander)

    generator = _read(SOURCE / "services" / "service_data-generator-e2e_v1.1.1.yaml")
    generator["service_id"] = "data-generator-dcems-control"
    generator["file_version"] = VERSION
    generator["service"]["grpc_bind"] = f"127.0.0.1:{GENERATOR_GRPC}"
    generator["recording"]["directory"] = str(root / "state" / "generator-records")
    _write(config / "services" / "service_data-generator-dcems-control_v1.0.0.yaml", generator)

    runtime = root / "state"
    definitions = {
        "gateway": ("industrial_gateway.main", "gateway-dcems-control-execution", EXECUTION_GRPC,
                    ["--project-root", str(config)], "WIND_HUB_E2E_GATEWAY_OPERATOR_TOKEN"),
        "gateway-target": ("industrial_gateway.main", "gateway-dcems-control-ingress", INGRESS_GRPC,
                           ["--project-root", str(config)], "WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"),
        "commander": ("commander", "commander-dcems-control", COMMANDER_GRPC, [],
                      "WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN"),
        "data-generator": ("data_generator.main", "data-generator-dcems-control", GENERATOR_GRPC, [],
                           "WIND_HUB_E2E_GENERATOR_CLI_TOKEN"),
    }
    services: dict[str, Any] = {}
    for name, (module, service_id, port, extra, token_env) in definitions.items():
        filename = f"service_{service_id}_v{VERSION}.yaml"
        services[name] = {
            "grpc_endpoint": f"127.0.0.1:{port}",
            "manager": "process",
            "target": name,
            "token_env": token_env,
            "process": {
                "argv": [sys.executable, "-m", module, "--config", str(config / "services" / filename), *extra],
                "cwd": str(ROOT),
                "state_dir": str(runtime / "cli-processes"),
                "log_file": str(runtime / f"{name}.log"),
            },
        }
    cli = {
        "schema_version": 1,
        "file_version": VERSION,
        "service_id": "cli-dcems-control",
        "cli": {
            "prompt": "dcems-control> ",
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
            "industrial_tcp_ports": [INGRESS_PORT],
        },
    }
    _write(config / "services" / "service_cli-dcems-control_v1.0.0.yaml", cli)
    _write(runtime / "cli.yaml", cli)

    _write(config / "deployments" / "deployment_dcems-control_v1.0.0.yaml", {
        "schema_version": 1,
        "file_version": VERSION,
        "deployment_id": DEPLOYMENT,
        "services": {
            service_id: {"config_id": service_id, "file_version": VERSION}
            for service_id in (
                "gateway-dcems-control-execution",
                "gateway-dcems-control-ingress",
                "commander-dcems-control",
                "data-generator-dcems-control",
                "cli-dcems-control",
            )
        },
        "instances": {
            "site": {"e2e-final": {"file_version": "1.0.0"}},
            "device": {"e2e-final": {"file_version": "1.0.1"}},
            "scenario": {"e2e-final": {"file_version": "1.0.0"}},
        },
    })
    return config


def main() -> None:
    """通过正式 CLI 使用的独立根准备控制链路。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:14223")
    args = parser.parse_args()
    print(prepare(args.output, nats_url=args.nats_url))


if __name__ == "__main__":
    main()
