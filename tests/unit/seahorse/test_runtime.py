"""验证一期随机模型的确定性、状态隔离和命令边界。"""
from pathlib import Path

import pytest
from pydantic import ValidationError

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import SimulationTime
from seahorse.infrastructure.config import SeahorseConfig, load_config
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
    config["turbines"][0]["points"]["emsPowAct"]["minimum"] = 3000
    with pytest.raises(ValidationError):
        SeahorseConfig.model_validate(config)


def test_control_feedback_is_not_measurement() -> None:
    turbine = RandomTurbine("t1", 1, {"ctrlActPow": (0, 100), "emsPowAct": (0, 100)})
    turbine.apply(EquipmentCommand("c1", "t1", "ctrlActPow", 35.0))
    assert turbine.snapshot().points["ctrlActPow"] == 35.0
    assert "emsPowAct" not in turbine.snapshot().points
    with pytest.raises(ValueError, match="unsupported"):
        turbine.apply(EquipmentCommand("c2", "t1", "missing", 1.0))


def test_clock_is_consecutive() -> None:
    runtime = create_runtime(load_config(CONFIG))
    assert runtime.step().time == SimulationTime(1, 0.5, 0.5)
    assert runtime.step().time == SimulationTime(2, 1.0, 0.5)
