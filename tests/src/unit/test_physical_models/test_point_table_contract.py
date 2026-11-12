"""出厂场景 103 点全链路契约：真实 Settings→Scenario→PhysicalEngine 发射值
与共享 Modbus 点表、units.yaml 单位目录、可写输入注册表逐点核对。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from data_generator.application.model.inputs import writable_inputs
from data_generator.application.physical_engine import PhysicalEngine
from data_generator.domain.model import validate_scalar
from data_generator.infrastructure.config import load_settings

pytestmark = [pytest.mark.unit]

SERVICE_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "services" / "data-generator-sim.example.yaml"
)
EXPECTED_COUNTS = {
    "WTG_SIM01": 32,
    "PV_SIM01": 16,
    "BESS_SIM01": 20,
    "GRID_SIM01": 15,
    "PPC_SIM01": 20,
}
EXPECTED_HOLDING = {
    "WTG_SIM01": 4,
    "PV_SIM01": 2,
    "BESS_SIM01": 3,
    "GRID_SIM01": 3,
    "PPC_SIM01": 6,
}


@pytest.fixture(scope="module")
def settings():
    return load_settings(SERVICE_PATH)


@pytest.fixture(scope="module")
def scenario(settings):
    return next(iter(settings.scenarios.values()))


@pytest.fixture(scope="module")
def tables(load_point_table, device_tables):
    return {device: load_point_table(filename) for device, filename in device_tables.items()}


@pytest.fixture(scope="module")
def valid_units(units_document):
    quantities = units_document["quantities"]
    names = set(quantities) | {body["base_unit"] for body in quantities.values()}
    names.update(units_document["units"])
    return names


def _counts(points) -> dict[str, int]:
    counts: dict[str, int] = {}
    for device, _point in points:
        counts[device] = counts.get(device, 0) + 1
    return counts


def test_shipped_scenario_declares_exactly_103_points(scenario) -> None:
    counts = _counts(scenario.points)
    assert counts == EXPECTED_COUNTS
    assert sum(counts.values()) == 103


def test_engine_emits_every_declared_point_once_and_nothing_extra(scenario) -> None:
    engine = PhysicalEngine(scenario)
    emitted = engine.advance(0.05, 1)
    assert set(emitted) == set(scenario.points)
    assert len(emitted) == 103
    assert _counts(emitted) == EXPECTED_COUNTS


def test_every_emitted_point_exists_in_shared_point_table(scenario, tables) -> None:
    engine = PhysicalEngine(scenario)
    emitted = engine.advance(0.05, 1)
    for (device, point), _value in emitted.items():
        assert point in tables[device], f"{device}.{point} missing from shared table"


def test_emitted_value_types_match_point_table_data_types_over_trajectory(scenario, tables) -> None:
    engine = PhysicalEngine(scenario)
    for step in range(1, 31):
        emitted = engine.advance(0.05, step)
        for (device, point), value in emitted.items():
            data_type = tables[device][point]["data_type"]
            # 生产校验器：bool 不得伪装成 int、浮点必须有限。
            validate_scalar(value, data_type)
            if data_type.startswith(("int", "uint")):
                assert type(value) is int, f"{device}.{point} expected int, got {type(value)}"
            elif data_type == "bool":
                assert type(value) is bool
            else:
                assert type(value) is float
                assert math.isfinite(value)


def test_every_point_table_unit_resolves_in_units_catalog(tables, valid_units) -> None:
    for device, table in tables.items():
        for point, body in table.items():
            assert body["unit"] in valid_units, f"{device}.{point} unit {body['unit']} unresolved"


def test_holding_registers_equal_writable_input_registry_one_to_one(scenario, tables) -> None:
    """18 个 holding 点必须与 inputs 注册表逐设备一一对应，且 data_type 一致。"""
    engine = PhysicalEngine(scenario)
    total_holding = 0
    for device, table in tables.items():
        holding = {
            point for point, body in table.items() if body["address"]["register_type"] == "holding"
        }
        total_holding += len(holding)
        assert len(holding) == EXPECTED_HOLDING[device]
        writable = writable_inputs(engine.kinds[device])
        assert holding == set(writable), f"{device} holding/input registry drift"
        for point in holding:
            assert table[point]["data_type"] == writable[point].data_type
    assert total_holding == 18


def test_published_holding_points_stay_writable_and_never_silent(scenario, tables) -> None:
    """103 点发布列表中的 holding 点必须仍是注册表内的可写控制输入。"""
    engine = PhysicalEngine(scenario)
    emitted = engine.advance(0.05, 1)
    for (device, point), _value in emitted.items():
        if tables[device][point]["address"]["register_type"] == "holding":
            assert point in writable_inputs(engine.kinds[device])


def test_two_engines_produce_bit_identical_trajectories(scenario) -> None:
    first = PhysicalEngine(scenario)
    second = PhysicalEngine(scenario)
    for step in range(1, 51):
        assert first.advance(0.05, step) == second.advance(0.05, step)


def test_shipped_yaml_device_refs_resolve_to_engine_models(scenario_document, scenario) -> None:
    document_devices = {
        entry["device_id"] for entry in scenario_document["scenarios"][0]["devices"]
    }
    assert set(EXPECTED_COUNTS) == document_devices
    engine = PhysicalEngine(scenario)
    assert set(engine.kinds) == document_devices
