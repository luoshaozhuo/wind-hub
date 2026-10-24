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


def test_implemented_command_points_are_accepted() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    commands = {
        "ctrlReActPow": 50.0,
        "emsActPowerSetInertia": 50.0,
        "emsActCtrlMode": 1,
        "emsReactPowerSetStatusWrite": 0,
        "ctrlActPow": 100.0,
        "emsActPowerChangeRate": 10.0,
        "ctrlStart": True,
        "ctrlStop": True,
        "ctrlReset": True,
    }
    for index, (point, value) in enumerate(commands.items()):
        turbine.apply(EquipmentCommand(str(index), "wt1", point, value))


@pytest.mark.parametrize(
    ("point", "value"),
    [
        ("emsActCtrlMode", 2),
        ("emsReactPowerSetStatusWrite", 1),
        ("emsActPowerSetInertia", -1.0),
    ],
)
def test_unconfirmed_control_values_are_rejected(point: str, value: float | int) -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    with pytest.raises(ValueError):
        turbine.apply(EquipmentCommand("unsupported", "wt1", point, value))


def test_inertia_mode_switches_active_power_target() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    with pytest.raises(ValueError, match="inertia target"):
        turbine.apply(EquipmentCommand("early", "wt1", "emsActCtrlMode", 1))
    turbine.apply(EquipmentCommand("normal", "wt1", "ctrlActPow", 100.0))
    turbine.apply(EquipmentCommand("inertia", "wt1", "emsActPowerSetInertia", 500.0))
    turbine.apply(EquipmentCommand("mode1", "wt1", "emsActCtrlMode", 1))
    turbine.step(SimulationTime(1, 0.5, 0.5), WIND, GRID)
    assert turbine.snapshot().points["emsActCtrlMode"] == 1
    assert turbine.snapshot().points["emsActPowerSetInertia"] == 500.0
    assert turbine.snapshot().points["emsActPowerSet"] == 100.0
    turbine.apply(EquipmentCommand("mode0", "wt1", "emsActCtrlMode", 0))
    turbine.step(SimulationTime(2, 1.0, 0.5), WIND, GRID)
    assert turbine.snapshot().points["emsActCtrlMode"] == 0


def test_reactive_status_reset_is_reflected_in_readback() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    turbine.apply(EquipmentCommand("reset", "wt1", "emsReactPowerSetStatusWrite", 0))
    turbine.step(SimulationTime(1, 0.5, 0.5), WIND, GRID)
    assert turbine.snapshot().points["emsReactPowerSetStatus"] == 0


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


def test_active_ramp_command_uses_kw_per_minute() -> None:
    turbine = SimpleWindTurbine("wt1", 17)
    turbine.apply(EquipmentCommand("ramp", "wt1", "emsActPowerChangeRate", 600.0))
    turbine.step(SimulationTime(1, 0.5, 0.5), WIND, GRID)
    assert 0 <= float(turbine.snapshot().points["emsPowAct"]) <= 5.0
    assert turbine.snapshot().points["emsActPowerChangeRate"] == 600.0
