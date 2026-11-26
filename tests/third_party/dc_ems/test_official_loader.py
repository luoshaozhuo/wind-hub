"""使用生产 Loader 检查 DCEMS 专用配置，而非只做 YAML 静态断言。"""

from __future__ import annotations

from pathlib import Path

from core.configuration.deployment import parse_deployment
from core.configuration.local_selection import select_local_service
from data_generator.infrastructure.config.service_settings import load_settings
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.config.resolution import load_gateway
from tests.third_party.dc_ems.stage_config import stage


def test_official_config_loaders_accept_ten_device_smoke(tmp_path: Path) -> None:
    """强制所有共享模板、设备/Scenario、Server 和 Deployment 真实解析。"""
    root = tmp_path / "dcems-smoke"
    stage(root)
    deployment = root / "deployments" / "deployment_dcems-smoke_v1.0.0.yaml"
    import yaml

    selected = parse_deployment(yaml.safe_load(deployment.read_text(encoding="utf-8")))
    assert selected.deployment_id == "dcems-smoke"
    assert len(selected.services) == 2
    gateway_service = root / "services" / "service_gateway-dcems-smoke_v1.0.0.yaml"
    generator_service = root / "services" / "service_data-generator-dcems-smoke_v1.0.0.yaml"
    assert select_local_service(gateway_service).deployment_id == "dcems-smoke"
    assert select_local_service(generator_service).deployment_id == "dcems-smoke"
    gateway = load_gateway(gateway_service, project_root=root)
    assert len(gateway.servers) == 10
    runtime = assemble_gateway(gateway)
    assert len(runtime.instances) == 10
    settings = load_settings(generator_service)
    assert "dcems-random-ten" in settings.scenarios
    scenario = settings.scenarios["dcems-random-ten"]
    assert len(scenario.devices) == 10
