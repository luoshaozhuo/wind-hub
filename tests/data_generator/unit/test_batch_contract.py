"""UR-03：DataBatch/标量编码的类型、时间与单来源契约（设计 §5、§9.3）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data_generator.domain.model import (
    DataBatch,
    DataPoint,
    GeneratorError,
    OutputMapping,
    OutputTarget,
    validate_scalar,
)

pytestmark = [pytest.mark.unit, pytest.mark.reliability, pytest.mark.invalid_data]

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _point(value: object = 1.0) -> DataPoint:
    return DataPoint("GW01", "p", value, NOW)  # type: ignore[arg-type]


def test_empty_batch_is_rejected() -> None:
    with pytest.raises(GeneratorError, match="empty batch"):
        DataBatch("gen-1", 1, NOW, ())


def test_nonempty_batch_carries_single_source_id() -> None:
    batch = DataBatch("gen-1", 7, NOW, (_point(), _point(2.0)))
    assert batch.source_id == "gen-1"
    assert batch.sequence == 7
    assert len(batch.points) == 2


def test_output_target_requires_nonempty_ids() -> None:
    OutputTarget("gw", "var")
    with pytest.raises(GeneratorError):
        OutputTarget("", "var")
    with pytest.raises(GeneratorError):
        OutputTarget("gw", "")


def test_targets_are_value_identities() -> None:
    assert len({OutputTarget("gw", "a"), OutputTarget("gw", "a")}) == 1
    mapping = OutputMapping("d", "p", OutputTarget("gw", "a"))
    assert mapping.target.variable_id == "a"


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        ("bool", True),
        ("int32", -1),
        ("uint16", 0),
        ("float32", 1.5),
        ("str", "ok"),
        ("bytes", b"ok"),
    ],
)
def test_accepted_scalars(data_type: str, value: object) -> None:
    validate_scalar(value, data_type)  # type: ignore[arg-type]


def test_bool_must_not_masquerade_as_integer() -> None:
    with pytest.raises(GeneratorError):
        validate_scalar(True, "int32")
    assert validate_scalar(True, "bool") is True


def test_non_finite_floats_rejected() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(GeneratorError, match="non-finite"):
            validate_scalar(value, "float64")


def test_integer_bounds_enforced() -> None:
    with pytest.raises(GeneratorError, match="range"):
        validate_scalar(2**31, "int32")
    with pytest.raises(GeneratorError, match="negative unsigned"):
        validate_scalar(-1, "uint8")
    validate_scalar(2**63 - 1, "int64")


def test_float32_overflow_rejected() -> None:
    with pytest.raises(GeneratorError, match="float32 overflow"):
        validate_scalar(3.5e38, "float32")


def test_minimum_maximum_contract() -> None:
    validate_scalar(5.0, "float64", minimum=0.0, maximum=10.0)
    with pytest.raises(GeneratorError, match="below minimum"):
        validate_scalar(-0.1, "float64", minimum=0.0)
    with pytest.raises(GeneratorError, match="above maximum"):
        validate_scalar(10.1, "float64", maximum=10.0)


def test_unknown_type_rejected() -> None:
    with pytest.raises(GeneratorError, match="unsupported scalar type"):
        validate_scalar(1, "int128")
