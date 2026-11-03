"""UR-01：随机模型确定性、边界与类型契约（设计 §10、表 13-1）。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.models import generate_values, random_value
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit, pytest.mark.reliability, pytest.mark.deterministic]


def test_same_seed_device_point_step_is_deterministic() -> None:
    params = {"distribution": "uniform", "minimum": 3.0, "maximum": 15.0, "data_type": "float64"}
    first = random_value(42, "GW01", "pitch", 7, params)
    second = random_value(42, "GW01", "pitch", 7, params)
    assert first == second


def test_independent_streams_change_with_step_and_point() -> None:
    params = {"minimum": 0.0, "maximum": 1.0, "data_type": "float64"}
    values = {random_value(1, "d", "p", step, params) for step in range(1, 50)}
    assert len(values) > 1


def test_device_order_independence() -> None:
    """同一 (seed,device,point,step) 的值不依赖其他设备是否先取值。"""
    params = {"minimum": 0.0, "maximum": 1.0, "data_type": "float64"}
    alone = random_value(7, "GW02", "p", 3, params)
    for step in range(1, 10):
        random_value(7, "GW01", "q", step, params)
    after_other_devices = random_value(7, "GW02", "p", 3, params)
    assert alone == after_other_devices


def test_uniform_values_stay_within_bounds() -> None:
    params = {"minimum": -2.0, "maximum": -1.0, "data_type": "float32"}
    for step in range(1, 200):
        value = random_value(9, "d", "p", step, params)
        assert -2.0 <= float(value) <= -1.0


@pytest.mark.parametrize(
    ("data_type", "lower", "upper"),
    [
        ("int32", -10, 10),
        ("uint16", 0, 65535),
        ("int64", -(2**40), 2**40),
    ],
)
def test_integer_streams_exact_type_and_bounds(data_type: str, lower: int, upper: int) -> None:
    params = {"minimum": lower, "maximum": upper, "data_type": data_type}
    for step in range(1, 50):
        value = random_value(3, "d", "p", step, params)
        assert type(value) is int
        assert lower <= value <= upper


def test_bool_stream_is_python_bool() -> None:
    params = {"data_type": "bool"}
    value = random_value(1, "d", "p", 1, params)
    assert type(value) is bool


def test_invalid_params_rejected_before_generation() -> None:
    with pytest.raises(GeneratorError):
        random_value(1, "d", "p", 1, {"minimum": 1, "maximum": 0, "data_type": "float64"})
    with pytest.raises(GeneratorError):
        random_value(1, "d", "p", 1, {"minimum": 0.0, "maximum": float("nan")})
    with pytest.raises(GeneratorError):
        random_value(1, "d", "p", 1, {"distribution": "gaussian"})
    with pytest.raises(GeneratorError, match="integer bounds"):
        random_value(1, "d", "p", 1, {"minimum": 0.0, "maximum": 1.0, "data_type": "int32"})


def test_step_must_be_positive() -> None:
    with pytest.raises(GeneratorError):
        random_value(1, "d", "p", 0, {"data_type": "float64"})


def test_generate_values_resolves_all_points_once() -> None:
    points = {
        ("GW01", "a"): {
            "data_type": "float64",
            "model": {"kind": "random", "minimum": 0.0, "maximum": 1.0},
        },
        ("GW01", "b"): {
            "data_type": "float64",
            "model": {"kind": "random", "minimum": 0.0, "maximum": 1.0},
        },
    }
    values = generate_values(5, 1, points)
    assert set(values) == set(points)
    assert all(math.isfinite(float(v)) for v in values.values())
