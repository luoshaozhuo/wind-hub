"""PointValue 不可变 Value Object 语义。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wind_hub_core.model.point import PointValue, Quality


def make_point_value(**overrides: object) -> PointValue:
    kwargs: dict[str, object] = {
        "device_id": "wt01",
        "point_id": "power",
        "value": 1.5,
    }
    kwargs.update(overrides)
    return PointValue(**kwargs)  # type: ignore[arg-type]


def test_construct_with_supported_scalar_types() -> None:
    for value in (1.5, 3, True, "on", None):
        pv = make_point_value(value=value)
        assert pv.value == value
        assert pv.quality is Quality.GOOD
        assert pv.timestamp.tzinfo is UTC
        assert pv.source is None


@pytest.mark.parametrize(
    "field", ["device_id", "point_id", "value", "quality", "timestamp", "source"]
)
def test_field_assignment_rejected(field: str) -> None:
    pv = make_point_value()
    with pytest.raises(ValidationError):
        setattr(pv, field, "x")


def test_model_copy_update_produces_new_instance() -> None:
    pv = make_point_value()
    stamped = pv.model_copy(update={"device_id": "wt02", "value": 2.5})
    assert stamped is not pv
    assert stamped.device_id == "wt02"
    assert stamped.value == 2.5
    assert pv.device_id == "wt01"
    assert pv.value == 1.5


def test_model_copy_update_timestamp() -> None:
    pv = make_point_value()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    copied = pv.model_copy(update={"timestamp": ts})
    assert copied.timestamp == ts
    assert pv.timestamp != ts


def test_equality_and_hash_by_value() -> None:
    a = make_point_value(timestamp=datetime(2026, 1, 1, tzinfo=UTC))
    b = make_point_value(timestamp=datetime(2026, 1, 1, tzinfo=UTC))
    assert a == b
    assert hash(a) == hash(b)


def test_shared_reference_is_safe() -> None:
    """容器共享引用后，任何"修改"只产生新对象，原对象不受影响。"""
    pv = make_point_value()
    container = [pv]
    mutated = container[0].model_copy(update={"value": 9.9})
    assert container[0].value == 1.5
    assert mutated.value == 9.9
