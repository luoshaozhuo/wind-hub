"""DCEMS 控制返回链路：生产配置加载器及隔离契约测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from commander.infrastructure.config.service_settings import load_service_settings
from core.configuration.deployment import parse_deployment
from core.configuration.local_selection import select_local_service
from data_generator.infrastructure.config.service_settings import (  # type: ignore[import-untyped]
    load_settings,
)
from industrial_gateway.infrastructure.config.resolution import (  # type: ignore[import-untyped]
    load_gateway,
)
from tests.third_party.dc_ems.prepare_control_chain import (
    COMMANDER_GRPC,
    DEPLOYMENT,
    EXECUTION_GRPC,
    GENERATOR_GRPC,
    INGRESS_GRPC,
    INGRESS_PORT,
    prepare,
)


def _load(config: Path, name: str) -> dict[str, Any]:
    """读取生成的版本化 YAML。"""
    return cast(
        dict[str, Any], yaml.safe_load((config / name).read_text(encoding="utf-8"))
    )


def test_control_config_uses_production_loaders(tmp_path: Path) -> None:
    """验证 Deployment 闭包和 Commander/Gateway/DG 正式 Loader。"""
    root = tmp_path / "dcems-control"
    config = prepare(root)
    deployment = parse_deployment(
        _load(config, "deployments/deployment_dcems-control_v1.0.0.yaml")
    )
    assert deployment.deployment_id == DEPLOYMENT
    assert len(deployment.services) == 5

    ingress = config / "services/service_gateway-dcems-control-ingress_v1.0.0.yaml"
    execution = config / "services/service_gateway-dcems-control-execution_v1.0.0.yaml"
    commander = config / "services/service_commander-dcems-control_v1.0.0.yaml"
    generator = config / "services/service_data-generator-dcems-control_v1.0.0.yaml"
    incoming = load_gateway(ingress, project_root=config)
    outgoing = load_gateway(execution, project_root=config)
    command_settings = load_service_settings(commander)
    generator_settings = load_settings(generator)
    assert len(incoming.servers) == 1
    assert len(outgoing.servers) == 5
    assert set(command_settings.simulation_targets) == {"PPC_SIM01"}
    assert "e2e-plant-physical" in generator_settings.scenarios
    for path in (ingress, execution, commander, generator):
        assert select_local_service(path).deployment_id == DEPLOYMENT


def test_control_identity_route_and_writable_register(tmp_path: Path) -> None:
    """核查实际点表、协议地址、权限及跨服务身份一致。"""
    config = prepare(tmp_path / "dcems-control")
    ingress = _load(config, "services/service_gateway-dcems-control-ingress_v1.0.0.yaml")
    execution = _load(config, "services/service_gateway-dcems-control-execution_v1.0.0.yaml")
    commander = _load(config, "services/service_commander-dcems-control_v1.0.0.yaml")
    generator = _load(config, "services/service_data-generator-dcems-control_v1.0.0.yaml")
    cli = _load(config, "../state/cli.yaml")
    incoming = ingress["servers"][0]
    executor = next(server for server in execution["servers"] if server["device_id"] == "PPC_SIM01")
    assert incoming["device_id"] == executor["device_id"] == "PPC_SIM01"
    assert incoming["listen_port"] == INGRESS_PORT
    assert incoming["control"]["enabled"] is True
    assert "simulation" not in incoming["control"]
    assert incoming["control"]["mappings"] == [
        {"variable_id": "agc_enable", "target_point_id": "agc_enable"}
    ]
    assert executor["control"]["mappings"] == incoming["control"]["mappings"]
    assert incoming["control"]["target"]["endpoint"] == f"127.0.0.1:{COMMANDER_GRPC}"
    assert commander["simulation_targets"]["PPC_SIM01"]["gateway_id"] == "gw-ppc-sim01"
    assert commander["simulation_targets"]["PPC_SIM01"]["endpoint"] == f"127.0.0.1:{EXECUTION_GRPC}"
    assert executor["control"]["simulation"]["endpoint"] == f"127.0.0.1:{GENERATOR_GRPC}"
    assert generator["service"]["grpc_bind"] == f"127.0.0.1:{GENERATOR_GRPC}"
    assert ingress["grpc"]["listen_port"] == INGRESS_GRPC

    point_table = _load(
        config, "templates/point_tables/point_table_modbus_plant_controller_sim_v1.2.0.yaml"
    )
    point = point_table["points"]["agc_enable"]
    assert point["address"]["register_type"] == "holding"
    assert point["address"]["address"] == 0
    assert point["address"]["count"] == 1
    assert point["data_type"] == "uint16"
    assert set(cli["services"]) == {"gateway", "gateway-target", "commander", "data-generator"}
    assert cli["services"]["commander"]["token_env"] == "WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN"
    assert {item["caller_id"] for item in ingress["security"]["callers"]} == {"operator"}


def test_control_root_refuses_reuse_and_measurement_is_unchanged(tmp_path: Path) -> None:
    """生成器绝不覆盖旧根，独立部署与十机测量完全分离。"""
    root = tmp_path / "dcems-control"
    config = prepare(root)
    with pytest.raises(FileExistsError):
        prepare(root)
    assert not (config / "services/service_gateway-dcems-target_v1.0.0.yaml").exists()
    assert not (config / "deployments/deployment_dcems-measurement_v1.0.0.yaml").exists()
    scenarios = _load(config, "instances/scenarios_e2e-final_v1.0.0.yaml")
    assert {item["scenario_id"] for item in scenarios["scenarios"]} == {"e2e-plant-physical"}
