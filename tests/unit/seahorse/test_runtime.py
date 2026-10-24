"""验证一期随机模型的确定性、状态隔离和命令边界。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import SimulationTime
from seahorse.infrastructure.config import RandomPoint, SeahorseConfig, load_config
from seahorse.infrastructure.factory import create_runtime
from seahorse.infrastructure.models.wind_turbine.random import RandomTurbine

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "seahorse.yaml"


def test_deterministic_snapshots() -> None:
    config = load_config(CONFIG)
    first = create_runtime(config)
    second = create_runtime(config)
    assert [first.step() for _ in range(5)] == [second.step() for _ in range(5)]


def test_independent_turbines_and_snapshot_isolation() -> None:
    runtime = create_runtime(load_config(CONFIG))
    snapshot = runtime.step()
    first = snapshot.by_id("d10001")
    second = snapshot.by_id("d10002")
    assert first.points != second.points
    old = dict(first.points)
    runtime.step()
    assert first.points == old


def test_configuration_validation() -> None:
    config = load_config(CONFIG).model_dump()
    config["turbines"][1]["device_id"] = config["turbines"][0]["device_id"]
    with pytest.raises(ValidationError):
        SeahorseConfig.model_validate(config)
    config["turbines"][1]["device_id"] = "d10002"
    config["turbines"][0]["model"] = "random"
    config["turbines"][0]["points"] = {"emsPowAct": {"minimum": 3000, "maximum": 2000}}
    with pytest.raises(ValidationError):
        SeahorseConfig.model_validate(config)


def test_control_feedback_is_not_measurement() -> None:
    turbine = RandomTurbine(
        "t1",
        1,
        {
            "ctrlActPow": RandomPoint(data_type="float32", minimum=0, maximum=100),
            "emsPowAct": RandomPoint(data_type="float32", minimum=0, maximum=100),
        },
    )
    turbine.apply(EquipmentCommand("c1", "t1", "ctrlActPow", 35.0))
    assert turbine.snapshot().points["ctrlActPow"] == 35.0
    assert "emsPowAct" not in turbine.snapshot().points
    with pytest.raises(ValueError, match="unsupported"):
        turbine.apply(EquipmentCommand("c2", "t1", "missing", 1.0))


def test_clock_is_consecutive() -> None:
    runtime = create_runtime(load_config(CONFIG))
    assert runtime.step().time == SimulationTime(1, 0.5, 0.5)
    assert runtime.step().time == SimulationTime(2, 1.0, 0.5)


def test_random_turbine_respects_scalar_types() -> None:
    from seahorse.domain.models import GridSample, WindSample

    turbine = RandomTurbine(
        "typed",
        5,
        {
            "flag": RandomPoint(data_type="bool", minimum=0, maximum=1),
            "count": RandomPoint(data_type="int32", minimum=-2, maximum=2),
            "energy": RandomPoint(data_type="uint64", minimum=2**53, maximum=2**53),
            "power": RandomPoint(data_type="float32", minimum=-4, maximum=4),
        },
    )
    for index in range(1, 16):
        turbine.step(
            SimulationTime(index, float(index), 1.0),
            WindSample(8.0, 270.0),
            GridSample(50.0, 35000.0, 0.0),
        )
        snapshot = turbine.snapshot().points
        assert type(snapshot["flag"]) is bool
        assert type(snapshot["count"]) is int and -2 <= snapshot["count"] <= 2
        assert type(snapshot["energy"]) is int and 2**53 <= snapshot["energy"] <= 2**53 + 10
        assert type(snapshot["power"]) is float


def test_random_point_rejects_incorrect_type_bounds() -> None:
    with pytest.raises(ValidationError):
        RandomPoint(data_type="int32", minimum=0.5, maximum=10)
    with pytest.raises(ValidationError):
        RandomPoint(data_type="uint64", minimum=-1, maximum=1)
    with pytest.raises(ValidationError):
        RandomPoint(data_type="float32", minimum=0, maximum=1e100)
    with pytest.raises(ValidationError):
        RandomPoint(data_type="uint64", minimum=0, maximum=2**53 + 1)


def test_dispatch_parameters_reach_simple_model() -> None:
    config = load_config(CONFIG).model_dump()
    config["dispatch_station"]["enabled"] = True
    config["dispatch_station"]["secondary_enabled"] = True
    config["dispatch_station"]["target_kw"] = 1200.0
    config["dispatch_station"]["reactive_target_kvar"] = 300.0
    runtime = create_runtime(SeahorseConfig.model_validate(config))
    snapshot = runtime.step().by_id("dispatch").points
    assert snapshot["emsGridDispatchAct"] == 1200.0
    assert snapshot["emsGridDispatchReact"] == 300.0


def test_dispatch_disabled_returns_zero_references() -> None:
    config = load_config(CONFIG).model_dump()
    config["dispatch_station"]["target_kw"] = 1500.0
    config["dispatch_station"]["reactive_target_kvar"] = 200.0
    points = create_runtime(SeahorseConfig.model_validate(config)).step().by_id("dispatch").points
    assert points == {"emsGridDispatchAct": 0.0, "emsGridDispatchReact": 0.0}
