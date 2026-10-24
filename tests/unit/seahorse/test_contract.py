"""Seahorse 设备数据契约回归，统一交 Claude 执行。"""

from seahorse.domain.models import GridSample, SimulationTime, WindSample
from seahorse.infrastructure.models.wind_turbine.contract import (
    COMMAND_POINTS,
    READ_POINTS,
    validate_turbine_points,
)
from seahorse.infrastructure.models.wind_turbine.history import TimeWindow
from seahorse.infrastructure.models.wind_turbine.simple import SimpleWindTurbine


def test_complete_turbine_contract() -> None:
    assert len(READ_POINTS) == 40
    assert len(COMMAND_POINTS) == 9
    turbine = SimpleWindTurbine("d10001", 42)
    turbine.step(
        SimulationTime(1, 0.5, 0.5),
        WindSample(8.0, 270.0),
        GridSample(50.0, 35000.0, 0.0),
    )
    validate_turbine_points(set(turbine.snapshot().points))


def test_time_window_irregular_sampling() -> None:
    window = TimeWindow(10)
    window.add(0.0, 0.0)
    window.add(2.0, 10.0)
    window.add(4.0, 20.0)
    assert window.average() == 5.0
