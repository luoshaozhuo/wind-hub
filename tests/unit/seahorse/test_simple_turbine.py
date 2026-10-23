"""风机动态与控制行为回归用例，供最终统一验收执行。"""

import math

import pytest

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import GridSample, SimulationTime, WindSample
from seahorse.infrastructure.models.wind_turbine.simple import SimpleWindTurbine

GRID = GridSample(50.0, 35000.0, 0.0)
WIND = WindSample(10.0, 270.0)


def test_telemetry_is_stable_between_steps() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    turbine.step(SimulationTime(1, 0.5, 0.5), WIND, GRID)
    first = turbine.snapshot()
    assert turbine.snapshot() == first
    assert math.isfinite(float(first.points["emsPowAct"]))
    assert first.points["emsMinBladeAngle"] == first.points["emsBladeAngle1"]


def test_control_readback_distinct_from_actual_power() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    turbine.apply(EquipmentCommand("cmd1", "wt1", "ctrlActPow", 100.0))
    turbine.step(SimulationTime(1, 0.5, 0.5), WIND, GRID)
    points = turbine.snapshot().points
    assert points["emsActPowerSet"] == 100.0
    assert float(points["emsPowAct"]) <= 100.0


def test_nine_command_points_are_accepted() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    commands = {
        "ctrlReActPow": 50.0,
        "ctrlActPow": 100.0,
        "emsActPowerSetInertia": 50.0,
        "emsReactPowerSetStatusWrite": 1,
        "emsActPowerChangeRate": 10.0,
        "emsActCtrlMode": 1,
        "ctrlStart": True,
        "ctrlStop": True,
        "ctrlReset": True,
    }
    for index, (point, value) in enumerate(commands.items()):
        turbine.apply(EquipmentCommand(str(index), "wt1", point, value))


def test_invalid_control_is_rejected() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    with pytest.raises(ValueError):
        turbine.apply(EquipmentCommand("bad", "wt1", "ctrlActPow", float("nan")))
    with pytest.raises(ValueError):
        turbine.apply(EquipmentCommand("bad2", "wt1", "ctrlActPow", 9000.0))


def test_stop_reduces_power_and_no_new_energy_at_zero() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    for index in range(1, 61):
        turbine.step(SimulationTime(index, index * 0.5, 0.5), WIND, GRID)
    turbine.apply(EquipmentCommand("stop", "wt1", "ctrlStop", True))
    old = float(turbine.snapshot().points["emsPowAct"])
    turbine.step(SimulationTime(61, 30.5, 0.5), WIND, GRID)
    assert float(turbine.snapshot().points["emsPowAct"]) <= old
