"""UR-01：静态物理模型 linear_table 插值、定义域与异常输入（设计 §10）。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.models import (
    generate_values,
    interpolation_table,
    static_value,
)
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit, pytest.mark.safety, pytest.mark.invalid_input]

TABLE = {
    "function": "linear_table",
    "input_point": "x",
    "values": [[0.0, 0.0], [1.0, 10.0], [2.0, 30.0]],
}


def test_table_requires_function_and_two_rows() -> None:
    with pytest.raises(GeneratorError):
        interpolation_table(
            {"function": "polynomial", "input_point": "x", "values": [[0, 0], [1, 1]]}
        )
    with pytest.raises(GeneratorError):
        interpolation_table({"function": "linear_table", "input_point": "x", "values": [[0, 0]]})


def test_table_abscissa_must_strictly_increase() -> None:
    with pytest.raises(GeneratorError, match="strictly increase"):
        interpolation_table(
            {"function": "linear_table", "input_point": "x", "values": [[0, 0], [1, 1], [1, 2]]}
        )


def test_table_rejects_non_finite_and_ill_typed_rows() -> None:
    with pytest.raises(GeneratorError):
        interpolation_table(
            {"function": "linear_table", "input_point": "x", "values": [[0, 0], [1, float("inf")]]}
        )
    with pytest.raises(GeneratorError):
        interpolation_table(
            {"function": "linear_table", "input_point": "x", "values": [[0, 0], ["1", 2]]}
        )


def test_interpolation_matches_defined_points() -> None:
    assert static_value({**TABLE, "data_type": "float64"}, {"x": 0.0}) == 0.0
    assert static_value({**TABLE, "data_type": "float64"}, {"x": 1.0}) == 10.0
    assert static_value({**TABLE, "data_type": "float64"}, {"x": 2.0}) == 30.0


def test_interpolation_linear_between_rows() -> None:
    assert static_value({**TABLE, "data_type": "float64"}, {"x": 0.5}) == pytest.approx(5.0)
    assert static_value({**TABLE, "data_type": "float64"}, {"x": 1.5}) == pytest.approx(20.0)


def test_out_of_range_rejected_by_default_and_clamped_when_configured() -> None:
    with pytest.raises(GeneratorError, match="outside defined range"):
        static_value({**TABLE, "data_type": "float64"}, {"x": 2.5})
    clamped = static_value({**TABLE, "out_of_range": "clamp", "data_type": "float64"}, {"x": 2.5})
    assert clamped == 30.0


def test_non_finite_or_missing_input_is_rejected() -> None:
    with pytest.raises(GeneratorError):
        static_value({**TABLE, "data_type": "float64"}, {"x": float("nan")})
    with pytest.raises(GeneratorError, match="missing"):
        static_value({**TABLE, "data_type": "float64"}, {})
    with pytest.raises(GeneratorError):
        static_value({**TABLE, "data_type": "float64"}, {"x": True})


def test_integer_result_must_be_integral() -> None:
    params = {
        "function": "linear_table",
        "input_point": "x",
        "values": [[0.0, 0.0], [1.0, 1.0]],
        "data_type": "int32",
    }
    assert type(static_value(params, {"x": 1.0})) is int
    with pytest.raises(GeneratorError, match="fractional"):
        static_value(params, {"x": 0.5})


def test_generate_values_orders_dependencies() -> None:
    points = {
        ("d", "x"): {
            "data_type": "float64",
            "model": {"kind": "random", "minimum": 1.0, "maximum": 1.0},
        },
        ("d", "y"): {
            "data_type": "float64",
            "model": {
                "kind": "static",
                "function": "linear_table",
                "input_point": "x",
                "values": [[0.0, 0.0], [2.0, 4.0]],
            },
        },
    }
    values = generate_values(1, 1, points)
    assert values[("d", "x")] == 1.0
    assert values[("d", "y")] == pytest.approx(2.0)


def test_cyclic_static_dependency_is_unresolvable() -> None:
    points = {
        ("d", "a"): {
            "data_type": "float64",
            "model": {
                "kind": "static",
                "function": "linear_table",
                "input_point": "b",
                "values": [[0, 0], [1, 1]],
            },
        },
        ("d", "b"): {
            "data_type": "float64",
            "model": {
                "kind": "static",
                "function": "linear_table",
                "input_point": "a",
                "values": [[0, 0], [1, 1]],
            },
        },
    }
    with pytest.raises(GeneratorError, match="unresolvable"):
        generate_values(1, 1, points)


def test_generated_values_are_finite() -> None:
    points = {
        ("d", "a"): {
            "data_type": "float32",
            "model": {"kind": "random", "minimum": 1e38, "maximum": 1e39},
        }
    }
    with pytest.raises(GeneratorError, match="float32 overflow"):
        generate_values(1, 1, points)
    assert math.isfinite(float(static_value({**TABLE, "data_type": "float64"}, {"x": 1.0})))
