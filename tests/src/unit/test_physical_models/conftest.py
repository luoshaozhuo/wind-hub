"""S2 物理模型测试共享夹具：直接加载出厂场景 YAML 的真实参数。

测试不经过完整 Configuration 加载链，但模型构造器拿到的 parameters / weather
字典与 ``configs/instances/data-generator-sim.yaml`` 出厂内容逐字一致，
PhysicalEngine 生产路径对同一字典的消费方式也完全相同。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from data_generator.application.model.weather import Weather

REPO_ROOT = Path(__file__).resolve().parents[4]
SCENARIO_PATH = REPO_ROOT / "configs" / "instances" / "data-generator-sim.yaml"
POINT_TABLE_DIR = REPO_ROOT / "configs" / "templates" / "point_tables"
UNITS_PATH = REPO_ROOT / "configs" / "templates" / "units.yaml"

# 出厂场景五台设备 → 共享 Modbus 点表文件（ppc 点表模板名为 plant_controller）。
DEVICE_TABLES = {
    "WTG_SIM01": "modbus_wtg_sim_1.0.0.yaml",
    "PV_SIM01": "modbus_pv_sim_1.0.0.yaml",
    "BESS_SIM01": "modbus_bess_sim_1.0.0.yaml",
    "GRID_SIM01": "modbus_grid_sim_1.0.0.yaml",
    "PPC_SIM01": "modbus_plant_controller_sim_1.0.0.yaml",
}


@pytest.fixture(scope="session")
def scenario() -> dict[str, Any]:
    with SCENARIO_PATH.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return document["scenarios"][0]


@pytest.fixture(scope="session")
def model_defs(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return scenario["models"]


@pytest.fixture(scope="session")
def device_points(scenario: dict[str, Any]) -> dict[str, list[str]]:
    return {entry["device_id"]: list(entry["points"]) for entry in scenario["devices"]}


@pytest.fixture
def make_params(model_defs: dict[str, dict[str, Any]]):
    def _factory(model_ref: str) -> dict[str, Any]:
        """返回深拷贝，允许测试变异参数而不污染其他用例。"""
        return copy.deepcopy(model_defs[model_ref]["parameters"])

    return _factory


@pytest.fixture
def make_weather_config(scenario: dict[str, Any]):
    def _factory(ref: str = "site-a") -> dict[str, Any]:
        return copy.deepcopy(scenario["weather"][ref])

    return _factory


@pytest.fixture
def make_weather():
    def _factory(
        *,
        wind_speed: float = 10.0,
        wind_direction: float = 0.0,
        air_density: float = 1.225,
        irradiance: float = 800.0,
        ambient_temperature: float = 20.0,
    ) -> Weather:
        return Weather(
            wind_speed=wind_speed,
            wind_direction=wind_direction,
            air_density=air_density,
            irradiance=irradiance,
            ambient_temperature=ambient_temperature,
        )

    return _factory


@pytest.fixture(scope="session")
def scenario_document() -> dict[str, Any]:
    with SCENARIO_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="session")
def device_tables() -> dict[str, str]:
    return dict(DEVICE_TABLES)


@pytest.fixture(scope="session")
def load_point_table():
    def _factory(filename: str) -> dict[str, dict[str, Any]]:
        with (POINT_TABLE_DIR / filename).open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)["points"]

    return _factory


@pytest.fixture(scope="session")
def units_document():
    with UNITS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)
