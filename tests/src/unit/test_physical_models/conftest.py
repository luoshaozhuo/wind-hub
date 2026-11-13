"""S2 物理模型测试共享夹具：加载权威配置树中的出厂仿真型号与服务场景。

测试不经过完整 Configuration 加载链，但模型构造器拿到的 parameters 与
``configs/templates/device_models/*_sim_physical_v1.0.0.yaml`` 中
``simulation.parameters`` 逐字一致——PhysicalEngine 生产路径经
``_configured_model_points`` 消费的正是同一份模板参数；天气配置与
``configs/services/data-generator-sim.example.yaml`` 场景内的 weather 块一致。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from data_generator.application.model.weather import Weather

REPO_ROOT = Path(__file__).resolve().parents[4]
SERVICE_PATH = REPO_ROOT / "configs" / "services" / "data-generator-sim.example.yaml"
DEVICE_MODEL_DIR = REPO_ROOT / "configs" / "templates" / "device_models"
POINT_TABLE_DIR = REPO_ROOT / "configs" / "templates" / "point_tables"
UNITS_PATH = REPO_ROOT / "configs" / "templates" / "units.yaml"

# 测试历史引用名 → 权威物理仿真设备型号模板（固定物理参数的唯一归属）。
PHYSICAL_MODEL_FILES = {
    "battery": "bess_sim_physical_v1.0.0.yaml",
    "solar": "pv_sim_physical_v1.0.0.yaml",
    "turbine": "wtg_sim_physical_v1.0.0.yaml",
    "grid": "grid_sim_physical_v1.0.0.yaml",
    "plant": "plant_controller_sim_physical_v1.0.0.yaml",
}

# 出厂场景五台设备 → 共享 Modbus 点表文件（ppc 点表模板名为 plant_controller）。
DEVICE_TABLES = {
    "WTG_SIM01": "modbus_wtg_sim_v1.0.0.yaml",
    "PV_SIM01": "modbus_pv_sim_v1.0.0.yaml",
    "BESS_SIM01": "modbus_bess_sim_v1.0.0.yaml",
    "GRID_SIM01": "modbus_grid_sim_v1.0.0.yaml",
    "PPC_SIM01": "modbus_plant_controller_sim_v1.0.0.yaml",
}


@pytest.fixture(scope="session")
def scenario_document() -> dict[str, Any]:
    with SERVICE_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="session")
def scenario(scenario_document: dict[str, Any]) -> dict[str, Any]:
    return scenario_document["scenarios"][0]


@pytest.fixture(scope="session")
def model_defs() -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for ref, filename in PHYSICAL_MODEL_FILES.items():
        with (DEVICE_MODEL_DIR / filename).open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        definitions[ref] = document["simulation"]
    return definitions


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
