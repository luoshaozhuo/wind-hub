"""随机模型分布级补充覆盖：constant/normal/bernoulli/choice/uniform_int、
hold_steps 窗口、分布与 data_type 不兼容拒绝（既有 test_random_model 未覆盖部分）。"""

from __future__ import annotations

import pytest

from data_generator.application.model.random import sample, validate_rule
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]


def test_constant_distribution_ignores_step_and_device() -> None:
    rule = {"distribution": "constant", "value": 7.5}
    values = {
        sample(1, f"dev{i}", "p", step, rule, "float64") for i in range(3) for step in range(1, 6)
    }
    assert values == {7.5}


def test_normal_distribution_reproducible_and_bounded_resample() -> None:
    rule = {
        "distribution": "normal",
        "mean": 10.0,
        "std": 1.0,
        "minimum": 0.0,
        "maximum": 20.0,
        "out_of_range": "resample",
        "max_attempts": 50,
    }
    first = [sample(9, "d", "p", step, rule, "float64") for step in range(1, 40)]
    second = [sample(9, "d", "p", step, rule, "float64") for step in range(1, 40)]
    assert first == second
    assert all(0.0 <= value <= 20.0 for value in first)


def test_normal_reject_sample_raises_when_bounds_impossible() -> None:
    # 均值本身在边界外，单次尝试必然越界 → fail-closed，不返回越界值。
    rule = {
        "distribution": "normal",
        "mean": 100.0,
        "std": 0.01,
        "minimum": 0.0,
        "maximum": 1.0,
        "out_of_range": "reject_sample",
    }
    with pytest.raises(GeneratorError):
        sample(3, "d", "p", 1, rule, "float64")


@pytest.mark.parametrize(
    "rule",
    [
        {"distribution": "normal", "mean": 0.0, "std": -1.0},
        {
            "distribution": "normal",
            "mean": 0.0,
            "std": 1.0,
            "out_of_range": "clamp",
        },
        {
            "distribution": "normal",
            "mean": 0.0,
            "std": 1.0,
            "out_of_range": "resample",
        },  # resample 缺 max_attempts
        {"distribution": "normal", "mean": 0.0, "std": 1.0, "minimum": 2.0, "maximum": 1.0},
    ],
)
def test_normal_rule_validation_rejects_illegal_contracts(rule) -> None:
    with pytest.raises(GeneratorError):
        validate_rule(rule, "float64")


def test_bernoulli_extreme_probabilities_are_deterministic() -> None:
    false_rule = {"distribution": "bernoulli", "probability": 0.0}
    true_rule = {"distribution": "bernoulli", "probability": 1.0}
    assert all(sample(5, "d", "p", step, false_rule, "bool") is False for step in range(1, 30))
    assert all(sample(5, "d", "p", step, true_rule, "bool") is True for step in range(1, 30))


def test_bernoulli_probability_out_of_range_rejected() -> None:
    for bad in (-0.1, 1.1):
        with pytest.raises(GeneratorError):
            validate_rule({"distribution": "bernoulli", "probability": bad}, "bool")


def test_choice_unweighted_and_weighted_stay_in_candidates() -> None:
    rule = {"distribution": "choice", "values": [1, 2, 3]}
    values = {sample(7, "d", "p", step, rule, "int32") for step in range(1, 60)}
    assert values <= {1, 2, 3} and values
    weighted = {"distribution": "choice", "values": [1, 2], "weights": [0.0, 1.0]}
    assert all(sample(7, "d", "p", step, weighted, "int32") == 2 for step in range(1, 30))


@pytest.mark.parametrize(
    "rule",
    [
        {"distribution": "choice", "values": []},
        {"distribution": "choice", "values": [1, 2], "weights": [1.0]},
        {"distribution": "choice", "values": [1, 2], "weights": [-1.0, 1.0]},
        {"distribution": "choice", "values": [1, 2], "weights": [0.0, 0.0]},
    ],
)
def test_choice_contract_violations_rejected(rule) -> None:
    with pytest.raises(GeneratorError):
        validate_rule(rule, "int32")


def test_uniform_int_emits_exact_integers_inside_bounds() -> None:
    rule = {"distribution": "uniform_int", "min": -3, "max": 4}
    for step in range(1, 100):
        value = sample(11, "d", "p", step, rule, "int16")
        assert type(value) is int and -3 <= value <= 4


def test_hold_steps_keeps_value_constant_within_window() -> None:
    rule = {
        "distribution": "uniform",
        "minimum": 0.0,
        "maximum": 1.0,
        "hold_steps": 4,
    }
    window_one = [sample(4, "d", "p", step, rule, "float64") for step in (1, 2, 3, 4)]
    window_two = [sample(4, "d", "p", step, rule, "float64") for step in (5, 6, 7, 8)]
    assert len(set(window_one)) == 1
    assert len(set(window_two)) == 1
    # 窗口键含 window 序号：两窗口分别恒定，跨窗口由独立哈希流决定。


def test_hold_steps_must_be_positive_integer() -> None:
    base = {"distribution": "constant", "value": 1.0, "hold_steps": 0}
    with pytest.raises(GeneratorError):
        validate_rule(base, "float64")
    base = {"distribution": "constant", "value": 1.0, "hold_steps": 2.0}
    with pytest.raises(GeneratorError):
        validate_rule(base, "float64")


@pytest.mark.parametrize(
    ("rule", "data_type"),
    [
        ({"distribution": "uniform", "minimum": 0.0, "maximum": 1.0}, "int32"),
        ({"distribution": "uniform_int", "min": 0, "max": 1}, "float32"),
        ({"distribution": "bernoulli", "probability": 0.5}, "float32"),
        ({"distribution": "normal", "mean": 0.0, "std": 1.0}, "int64"),
        ({"distribution": "bogus"}, "float64"),
    ],
)
def test_distribution_incompatible_with_data_type_rejected(rule, data_type) -> None:
    with pytest.raises(GeneratorError):
        validate_rule(rule, data_type)


def test_seed_must_be_integer_and_step_positive() -> None:
    rule = {"distribution": "uniform", "minimum": 0.0, "maximum": 1.0}
    with pytest.raises(GeneratorError):
        sample(1.5, "d", "p", 1, rule, "float64")  # type: ignore[arg-type]
    with pytest.raises(GeneratorError):
        sample(1, "d", "p", 0, rule, "float64")
