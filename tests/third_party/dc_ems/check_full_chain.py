"""使用生产 Loader 验证双 Gateway、Collector 和 Sink 配置闭包。"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from collector.infrastructure.config.validation import (
    resolve_source_paths,
    validate_collector_snapshot,
)
from core.configuration.deployment import parse_deployment
from core.configuration.local_selection import select_local_service
from data_generator.infrastructure.config.service_settings import (  # type: ignore[import-untyped]
    load_settings,
)
from industrial_gateway.infrastructure.config.resolution import (  # type: ignore[import-untyped]
    load_gateway,
)
from sink_service.infrastructure.config_io import read_snapshot  # type: ignore[import-untyped]
from tests.third_party.dc_ems.prepare_full_chain import STATION_POINTS

POINTS = frozenset(
    {
        "wind_speed",
        "sim_running_status",
        "sim_active_power",
        "sim_reactive_power",
        "device_fault_active",
        "active_power_limit",
        "generator_speed",
        "blade_1_pitch_angle",
        "pitch_angle",
    }
)


def check(root: Path) -> None:
    """检查所有源、目标和 Sink Delivery 均有明确的逐点契约。"""
    root = root.resolve(strict=True)
    config = root / "configs"
    deployment_path = config / "deployments" / "deployment_dcems-measurement_v1.0.0.yaml"
    deployment = parse_deployment(yaml.safe_load(deployment_path.read_text(encoding="utf-8")))
    assert deployment.deployment_id == "dcems-measurement"
    assert len(deployment.services) == 5

    source_file = config / "services" / "service_gateway-dcems-source_v1.0.0.yaml"
    target_file = config / "services" / "service_gateway-dcems-target_v1.0.0.yaml"
    source = load_gateway(source_file, project_root=config)
    target = load_gateway(target_file, project_root=config)
    assert len(source.servers) == len(target.servers) == 10
    assert {server.settings.device_id for server in source.servers.values()} == {
        server.settings.device_id for server in target.servers.values()
    }
    assert {server.settings.listen_port for server in source.servers.values()}.isdisjoint(
        {server.settings.listen_port for server in target.servers.values()}
    )
    assert select_local_service(source_file).deployment_id == deployment.deployment_id
    assert select_local_service(target_file).deployment_id == deployment.deployment_id

    generator_file = config / "services" / "service_data-generator-dcems-measurement_v1.0.0.yaml"
    generator = load_settings(generator_file)
    scenario = generator.scenarios["dcems-random-ten"]
    assert len(scenario.devices) == 10

    scenarios = yaml.safe_load(
        (config / "instances" / "scenarios_dcems-smoke_v1.0.0.yaml").read_text(encoding="utf-8")
    )["scenarios"]
    identity = next(
        (item for item in scenarios if item["scenario_id"] == "dcems-identity-ten"), None
    )
    assert identity is not None, "identity isolation scenario is missing"
    assert len(identity["devices"]) == len(identity["gateways"]) == 10
    assert (
        len(
            {
                identity["models"][item["model_ref"]]["rules"]["wind_speed"]["value"]
                for item in identity["devices"]
            }
        )
        == 10
    )

    collector_file = config / "services" / "service_collector-dcems-realtime_v2.0.0.yaml"
    collector_paths = resolve_source_paths(collector_file)
    validate_collector_snapshot(collector_paths)
    source_devices = yaml.safe_load(
        (config / "instances" / "device_dcems-source_v1.0.0.yaml").read_text(encoding="utf-8")
    )["devices"]
    target_devices = yaml.safe_load(
        (config / "instances" / "device_dcems-target_v1.0.0.yaml").read_text(encoding="utf-8")
    )["devices"]
    assert set(source_devices) == set(target_devices)
    assert set(source_devices) == {f"WTG{index:02d}" for index in range(1, 11)}

    sink = read_snapshot(
        root,
        config / "services" / "service_sink-dcems-measurement_v2.0.0.yaml",
    )
    instances = yaml.safe_load(
        (config / "instances" / "deliveries_dcems-measurement_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["deliveries"]
    outputs = yaml.safe_load(
        (config / "instances" / "sinks_dcems-measurement_v1.0.0.yaml").read_text(encoding="utf-8")
    )["sinks"]
    assert len(instances) == len(outputs) == 10
    assert sink.service["wind_farm_id"] == "dcems-smoke"
    for index in range(1, 11):
        device = f"WTG{index:02d}"
        points = POINTS | STATION_POINTS if index == 1 else POINTS
        delivery = instances[f"dcems_{device.lower()}"]
        assert delivery["source"]["devices"] == [device]
        assert set(delivery["source"]["points"]) == points
        assert delivery["source"]["collector_id"] == "collector-dcems-realtime"
        assert delivery["desired_state"] == "START"
        assert {
            (item["source_device_id"], item["source_point_id"], item["variable_id"])
            for item in delivery["target_mappings"]
        } == {(device, point, point) for point in points}
        output = outputs[delivery["sink_id"]]
        assert output["connection"]["gateway_id"] == f"dcems-target-{index:02d}"
        assert output["connection"]["endpoint"] == "127.0.0.1:52075"
        assert source_devices[device]["endpoint"]["port"] == 15220 + index
        assert target_devices[device]["endpoint"]["port"] == 15120 + index


def main() -> None:
    """在无真实中间件时进行完整配置验证。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    check(args.root)


if __name__ == "__main__":
    main()
