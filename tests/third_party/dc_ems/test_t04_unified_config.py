"""T04 统一根离线契约：十机测量 + 单机控制共享同一身份与端口拓扑。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.validation import (
    resolve_source_paths,
    validate_collector_snapshot,
)
from core.configuration.deployment import parse_deployment
from core.configuration.local_selection import select_local_service
from data_generator.infrastructure.config.service_settings import (
    load_settings,  # type: ignore[import-untyped]
)
from industrial_gateway.infrastructure.config.resolution import (
    load_gateway,  # type: ignore[import-untyped]
)
from sink_service.infrastructure.config_io import read_snapshot  # type: ignore[import-untyped]
from tests.third_party.dc_ems.prepare_seed import load_mapping
from tests.third_party.dc_ems.validate_gateway_mapping import validate

ROOT = Path(__file__).resolve().parents[3]
MAPPING = ROOT / "tests" / "third_party" / "dc-ems" / "config" / "mapping_random_smoke.csv"
MEASUREMENT_POINTS = frozenset(
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


def _root() -> Path:
    return Path(__file__).resolve().parents[3] / "run" / "dcems-t04"


pytestmark = pytest.mark.skipif(
    not (_root() / "configs").is_dir(),
    reason="T04 unified root not prepared",
)


def _config() -> Path:
    return _root() / "configs"


def test_deployment_closes_six_services_and_ten_devices() -> None:
    config = _config()
    deployment = parse_deployment(
        yaml.safe_load(
            (config / "deployments" / "deployment_dcems-t04_v1.0.0.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    assert deployment.deployment_id == "dcems-t04"
    assert len(deployment.services) == 6
    source = load_gateway(
        config / "services" / "service_gateway-dcems-source_v1.0.0.yaml", project_root=config
    )
    target = load_gateway(
        config / "services" / "service_gateway-dcems-target_v1.0.0.yaml", project_root=config
    )
    assert len(source.servers) == len(target.servers) == 10
    assert {s.settings.device_id for s in source.servers.values()} == {
        f"WTG{index:02d}" for index in range(1, 11)
    }
    assert {s.settings.device_id for s in target.servers.values()} == {
        f"WTG{index:02d}" for index in range(1, 11)
    }
    source_ports = {s.settings.listen_port for s in source.servers.values()}
    target_ports = {s.settings.listen_port for s in target.servers.values()}
    assert source_ports == set(range(15221, 15231))
    assert target_ports == set(range(15121, 15131))
    assert source_ports.isdisjoint(target_ports)
    assert (
        select_local_service(
            config / "services" / "service_gateway-dcems-source_v1.0.0.yaml"
        ).deployment_id
        == deployment.deployment_id
    )


def test_read_merge_max_gap_is_zero_in_effective_device_config() -> None:
    """在共享快照合成后的生效协议参数中断言（设备>型号>协议模板默认）。"""
    from core.configuration import SharedConfigPaths, SharedDocumentLoader, build_shared_snapshot
    from core.configuration.effective import resolve_effective_device
    from core.domain import ConnectionEndpoint
    from core.infrastructure.protocol.modbus.config import parse_modbus_config

    config = _config()
    for devices_file in ("device_dcems-source_v1.0.0.yaml", "device_dcems-target_v1.0.0.yaml"):
        documents = SharedDocumentLoader(
            SharedConfigPaths(
                instances=config / "instances",
                templates=config / "templates",
                site_file=config / "instances" / "site_dcems-smoke_v1.0.0.yaml",
                devices_file=config / "instances" / devices_file,
            )
        ).load()
        snapshot = build_shared_snapshot(documents)
        assert len(snapshot.site.devices) == 10
        for device_id, device in snapshot.site.devices.items():
            model = snapshot.device_models[device.device_model_id]
            table = snapshot.point_tables[model.point_table_id]
            protocol = table.protocol.name
            effective, options = resolve_effective_device(device, model, protocol)
            assert (
                options.get("read_merge_max_gap") == 0
            ), f"{devices_file}:{device_id} effective gap={options.get('read_merge_max_gap')}"
            parsed = parse_modbus_config(
                ConnectionEndpoint(host=effective.endpoint.host, port=effective.endpoint.port),
                options,
            )
            assert parsed.read_merge_max_gap == 0


def test_scenario_is_mixed_physical_and_random_ten() -> None:
    generator = load_settings(
        _config() / "services" / "service_data-generator-dcems-t04_v1.0.0.yaml"
    )
    scenario = generator.scenarios["t04-unified-ten"]
    assert len(scenario.devices) == 10
    assert scenario.bindings["WTG01"]["model_ref"] == "WTG01"
    assert scenario.models["WTG01"]["kind"] == "physical"
    for index in range(2, 11):
        device = f"WTG{index:02d}"
        assert scenario.models[scenario.bindings[device]["model_ref"]]["kind"] == "random"
    # 物理 WTG01 的场景点默认取物理点表全部点，且必须可由物理引擎提供。
    wtg01_points = {p for d, p in scenario.points if d == "WTG01"}
    assert len(wtg01_points) >= 30
    assert {"wind_speed", "active_power", "reactive_power", "fault_status"} <= wtg01_points
    assert scenario.models["WTG01"]["weather_ref"] == "site-wtg01"


def test_deliveries_map_nine_points_with_renames() -> None:
    snapshot = read_snapshot(
        _root(), _config() / "services" / "service_sink-dcems-measurement_v2.0.0.yaml"
    )
    assert snapshot.service["wind_farm_id"] == "dcems-smoke"
    deliveries = {
        item["source"]["devices"][0]: item
        for item in yaml.safe_load(
            (_config() / "instances" / "deliveries_dcems-measurement_v1.0.0.yaml").read_text(
                encoding="utf-8"
            )
        )["deliveries"].values()
    }
    assert len(deliveries) == 10
    wtg01 = deliveries["WTG01"]
    source_points = set(wtg01["source"]["points"])
    assert source_points == {
        "wind_speed",
        "active_power",
        "reactive_power",
        "running_status",
        "fault_status",
        "power_limit_setpoint",
        "sim_generator_speed",
        "pitch_angle_1",
    }
    targets = {item["source_point_id"]: item["variable_id"] for item in wtg01["target_mappings"]}
    assert targets["active_power"] == "sim_active_power"
    assert targets["fault_status"] == "device_fault_active"
    assert (
        sum(1 for item in wtg01["target_mappings"] if item["source_point_id"] == "pitch_angle_1")
        == 2
    )
    for index in range(2, 11):
        delivery = deliveries[f"WTG{index:02d}"]
        assert set(delivery["source"]["points"]) == MEASUREMENT_POINTS
        assert {
            item["source_point_id"] for item in delivery["target_mappings"]
        } == MEASUREMENT_POINTS


def test_unified_point_table_matches_mapping_and_control_contract() -> None:
    config = _config()
    table = yaml.safe_load(
        (
            config / "templates" / "point_tables" / "point_table_dcems_wtg_unified_v1.0.0.yaml"
        ).read_text(encoding="utf-8")
    )
    mapping = load_mapping(MAPPING)
    points = table["points"]
    # 映射 CSV 的键是 DCEMS varPath；点表键是 wind-hub 业务点身份。
    assert {row["business_point_id"] for row in mapping.values()} <= set(points)
    for var_path, spec in mapping.items():
        point = points[spec["business_point_id"]]
        chunks = spec["modbus_addr"].split(".")
        assert point["address"]["register_type"] == {"3": "input", "1": "discrete_input"}[chunks[1]]
        assert point["address"]["address"] == int(chunks[2])
        assert point["address"].get("word_order", "big_endian") == (
            "little_endian" if spec.get("high_bit") == "1" else "big_endian"
        )
        assert var_path
    control = {
        "active_power_setpoint": 41557,
        "reactive_power_setpoint": 41555,
    }
    for name, address in control.items():
        point = points[name]
        assert point["address"]["register_type"] == "holding"
        assert point["address"]["address"] == address
        assert point["address"]["word_order"] == "little_endian"
        assert point["data_type"] == "float32"
        assert point["point_groups"] == ["control"]
    # DCEMS 派生种子（若已生成）的9 点测量部分必须与点表闭合。
    seed_path = _root() / "server" / "data.json"
    if seed_path.is_file():
        import json

        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        measurement = {
            "edgeaddrset": [row for row in seed["edgeaddrset"] if row["varPath"] in set(mapping)],
        }
        validate(measurement, mapping, table)


def test_commander_targets_wtg01_on_source_gateway() -> None:
    commander = yaml.safe_load(
        (_config() / "services" / "service_commander-dcems-t04_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )
    target = commander["simulation_targets"]["WTG01"]
    assert target["endpoint"] == "127.0.0.1:52073"
    assert target["gateway_id"] == "dcems-source-01"
    assert commander["grpc"]["listen_port"] == 52152
    assert commander["audit"]["nats_servers"] == ["nats://127.0.0.1:14222"]
    source = yaml.safe_load(
        (_config() / "services" / "service_gateway-dcems-source_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )
    wtg01 = next(s for s in source["servers"] if s["device_id"] == "WTG01")
    assert wtg01["control"]["enabled"] is True
    assert wtg01["control"]["simulation"]["endpoint"] == "127.0.0.1:52061"
    target_gw = yaml.safe_load(
        (_config() / "services" / "service_gateway-dcems-target_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )
    wtg01_t = next(s for s in target_gw["servers"] if s["device_id"] == "WTG01")
    assert wtg01_t["control"]["enabled"] is True
    assert "simulation" not in wtg01_t["control"]
    assert wtg01_t["control"]["mappings"][0]["value_scale"] == 1000.0
    for server in target_gw["servers"]:
        if server["device_id"] != "WTG01":
            assert server["control"]["enabled"] is False


def test_collector_and_sink_snapshots_close() -> None:
    config = _config()
    deployment = parse_deployment(
        yaml.safe_load(
            (config / "deployments" / "deployment_dcems-t04_v1.0.0.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    collector_file = config / "services" / "service_collector-dcems-realtime_v2.0.0.yaml"
    paths = resolve_source_paths(collector_file)
    validate_collector_snapshot(paths)
    assert deployment.deployment_id == "dcems-t04"
