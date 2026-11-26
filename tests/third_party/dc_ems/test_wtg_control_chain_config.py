"""DCEMS 风机控制入口的隔离配置闭包与单位契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from commander.infrastructure.config.service_settings import load_service_settings
from core.application.errors import ConfigError
from data_generator.infrastructure.config.service_settings import (  # type: ignore[import-untyped]
    load_settings,
)
from industrial_gateway.infrastructure.config.resolution import (  # type: ignore[import-untyped]
    load_gateway,
)
from industrial_gateway.infrastructure.config.schema import TargetMapping
from tests.third_party.dc_ems.prepare_wtg_control_chain import CONTROL_PORT, prepare


def _read(path: Path) -> dict[str, Any]:
    """读取隔离根中的显式 YAML 文件。"""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _write(path: Path, document: dict[str, Any]) -> None:
    """仅修改 pytest 私有配置根。"""
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_wtg_dcems_loader_and_control_contract(tmp_path: Path) -> None:
    """使用生产 Loader 解析两侧 Gateway、Commander、DG。"""
    root = tmp_path / "dcems-control-wtg"
    config = prepare(root)
    ingress = config / "services/service_gateway-dcems-control-ingress_v1.0.0.yaml"
    execution = config / "services/service_gateway-dcems-control-execution_v1.0.0.yaml"
    commander = config / "services/service_commander-dcems-control_v1.0.0.yaml"
    generator = config / "services/service_data-generator-dcems-control_v1.0.0.yaml"
    loaded_ingress = load_gateway(ingress, project_root=config)
    loaded_execution = load_gateway(execution, project_root=config)
    command_settings = load_service_settings(commander)
    generator_settings = load_settings(generator)

    assert len(loaded_ingress.servers) == 2
    assert len(loaded_execution.servers) == 5
    assert set(command_settings.simulation_targets) == {"PPC_SIM01", "WTG_SIM01"}
    assert "e2e-plant-physical" in generator_settings.scenarios
    source = loaded_ingress.servers["dcems-control-ingress-wtg"]
    assert source.settings.device_id == "WTG_SIM01"
    assert source.settings.listen_port == CONTROL_PORT
    mappings = {
        item.variable_id: item
        for item in source.settings.control.mappings
    }
    assert set(mappings) == {"active_power_setpoint", "reactive_power_setpoint"}
    assert all(item.value_scale == 1000.0 for item in mappings.values())

    points = source.variables
    assert points["active_power_setpoint"].address["address"] == 41557
    assert points["reactive_power_setpoint"].address["address"] == 41555
    for point_id in mappings:
        point = points[point_id]
        assert point.address["register_type"] == "holding"
        assert point.address["word_order"] == "little_endian"
        assert point.data_type == "float32"
        assert point.unit in {"watt", "var"}

    simulation = loaded_execution.servers["gw-wtg-sim01"]
    assert simulation.settings.control.enabled
    assert simulation.settings.control.simulation is not None
    assert all(
        item.value_scale == 1.0 for item in simulation.settings.control.mappings
    )
    assert command_settings.simulation_targets["WTG_SIM01"].gateway_id == "gw-wtg-sim01"

    scenarios = _read(config / "instances/scenarios_e2e-final_v1.0.0.yaml")
    scenario = next(
        item for item in scenarios["scenarios"]
        if item["scenario_id"] == "e2e-plant-physical"
    )
    wtg = next(item for item in scenario["devices"] if item["device_id"] == "WTG_SIM01")
    assert wtg["controller_mode"] == "local"
    model = _read(
        config / "templates/device_models/device_model_plant_controller_dcems_wtg_v1.2.0.yaml"
    )
    params = model["simulation"]["parameters"]
    assert "WTG_SIM01" not in params["participating_device_ids"]
    assert "WTG_SIM01" not in params["allocation_weights"]


@pytest.mark.parametrize("bad_scale", [0, -1, float("inf"), float("nan")])
def test_scaled_mapping_rejects_invalid_factor(bad_scale: float) -> None:
    """禁用负值、零及非有限的转换因子。"""
    with pytest.raises(ValueError):
        TargetMapping(
            variable_id="active_power_setpoint",
            target_point_id="active_power_setpoint",
            value_scale=bad_scale,
        )


def test_scaled_integer_command_is_rejected_by_production_loader(tmp_path: Path) -> None:
    """控制映射只允许对浮点型值进行倍率转换。"""
    config = prepare(tmp_path / "dcems-control-wtg")
    path = config / "services/service_gateway-dcems-control-ingress_v1.0.0.yaml"
    data = _read(path)
    wind = next(item for item in data["servers"] if item["device_id"] == "WTG_SIM01")
    wind["control"]["mappings"] = [{
        "variable_id": "control_mode",
        "target_point_id": "control_mode",
        "value_scale": 1000.0,
    }]
    _write(path, data)
    with pytest.raises(ConfigError, match="floating-point"):
        load_gateway(path, project_root=config)
