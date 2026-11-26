"""完整测量链路的配置和映射回归，不要求 DCEMS 镜像。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.third_party.dc_ems.check_full_chain import POINTS, check
from tests.third_party.dc_ems.prepare_full_chain import STATION_POINTS, prepare
from tests.third_party.dc_ems.prepare_full_chain_cli import prepare_cli


def test_full_chain_production_loaders(tmp_path: Path) -> None:
    """逐项加载真实 Gateway、Collector、DG、Sink 和 Deployment。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root)
    check(root)
    cli = yaml.safe_load(prepare_cli(config, root / "state").read_text(encoding="utf-8"))
    assert set(cli["services"]) == {
        "gateway",
        "gateway-target",
        "collector",
        "sink",
        "data-generator",
    }
    assert cli["services"]["sink"]["process"]["argv"][-1].endswith(
        "service_sink-dcems-measurement_v2.0.0.yaml"
    )
    assert cli["services"]["collector"]["process"]["argv"][2] == "collector.main"


def test_isolated_templates_and_no_target_collection(tmp_path: Path) -> None:
    """站控只属于首台；目标设备必须和 Collector 源组隔离。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root)
    templates = config / "templates"
    full = yaml.safe_load(
        (templates / "point_tables" / "point_table_dcems_wtg_random_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["points"]
    compact = yaml.safe_load(
        (templates / "point_tables" / "point_table_dcems_wtg_measurement_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["points"]
    assert set(full) == POINTS | STATION_POINTS
    assert set(compact) == POINTS
    business_points = sorted(templates.glob("business_points_standard_v*.yaml"))
    assert [path.name for path in business_points] == ["business_points_standard_v1.6.0.yaml"]
    sources = yaml.safe_load(
        (config / "instances" / "device_dcems-source_v1.0.0.yaml").read_text(encoding="utf-8")
    )["devices"]
    targets = yaml.safe_load(
        (config / "instances" / "device_dcems-target_v1.0.0.yaml").read_text(encoding="utf-8")
    )["devices"]
    assert {value["group"] for value in sources.values()} == {"dcems-source"}
    assert {value["group"] for value in targets.values()} == {"dcems-target"}
    assert all(
        value["overrides"]["protocol_options"]["read_merge_max_gap"] == 0
        for value in [*sources.values(), *targets.values()]
    ), "sparse point tables must not merge reads across undefined registers"
    assert sources["WTG01"]["model"]["id"] == "dcems_wtg_random"
    assert all(
        sources[f"WTG{index:02d}"]["model"]["id"] == "dcems_wtg_measurement"
        for index in range(2, 11)
    )
    tasks = yaml.safe_load(
        (config / "instances" / "collector_tasks_dcems-source_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["tasks"]
    assert {value["group"] for value in tasks.values()} == {"dcems-source"}


def test_full_chain_refuses_overwrite(tmp_path: Path) -> None:
    """禁止覆盖已启动或已生成的隔离配置与运行状态。"""
    root = tmp_path / "dcems-measurement"
    prepare(root)
    with pytest.raises(FileExistsError):
        prepare(root)


def test_prepare_supports_production_default_gap(tmp_path: Path) -> None:
    """整改验证必须能在生产默认 gap=8 下生成同一隔离配置。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root, read_merge_max_gap=8)
    check(root)
    devices = yaml.safe_load(
        (config / "instances" / "device_dcems-source_v1.0.0.yaml").read_text(encoding="utf-8")
    )["devices"]
    assert all(
        value["overrides"]["protocol_options"]["read_merge_max_gap"] == 8
        for value in devices.values()
    )


def test_prepare_can_route_collector_through_request_probe(tmp_path: Path) -> None:
    """记录真实请求时必须把 Collector 源端端口与 Gateway 监听端口分离。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root, source_port_base=15231)
    devices = yaml.safe_load(
        (config / "instances" / "device_dcems-source_v1.0.0.yaml").read_text(encoding="utf-8")
    )["devices"]
    assert [value["endpoint"]["port"] for value in devices.values()] == list(range(15231, 15241))
    gateway = yaml.safe_load(
        (config / "services" / "service_gateway-dcems-source_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert [server["listen_port"] for server in gateway["servers"]] == list(range(15221, 15231))


def test_identity_scenario_has_distinct_constants(tmp_path: Path) -> None:
    """防串台激励必须让每台风机具有唯一且可断言的静态差异。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root)
    scenarios = yaml.safe_load(
        (config / "instances" / "scenarios_dcems-smoke_v1.0.0.yaml").read_text(encoding="utf-8")
    )["scenarios"]
    identity = next(item for item in scenarios if item["scenario_id"] == "dcems-identity-ten")
    assert len(identity["devices"]) == len(identity["gateways"]) == 10
    winds = {
        identity["models"][item["model_ref"]]["rules"]["wind_speed"]["value"]
        for item in identity["devices"]
    }
    powers = {
        identity["models"][item["model_ref"]]["rules"]["sim_active_power"]["value"]
        for item in identity["devices"]
    }
    assert len(winds) == len(powers) == 10
    assert all("sim_plant_active_power" in item["points"] for item in identity["devices"][:1])
    assert not any("sim_plant_active_power" in item["points"] for item in identity["devices"][1:])
