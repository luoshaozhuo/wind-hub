"""点表/业务点领域模型独立不变量测试（point.py）。

- BusinessPoint/Point/PointTable 的去空白、空值、scale/offset 有限性；
- ext 标量约束、point_groups 去重；
- engineering/raw 双变换对 None/bool/str/数值的分支；
- PointTable 键一致性、parent_id 约束与查询方法。
"""

from __future__ import annotations

import math
from types import MappingProxyType

import pytest

from core.domain.point import (
    BusinessPoint,
    Point,
    PointTable,
    engineering_value,
    raw_write_value,
)
from core.domain.value_objects import DataType, PointAccess, Protocol
from tests.support.units import RPM_UNIT


def _point(**overrides) -> Point:
    values = {
        "point_id": "wind_speed",
        "business_point_id": "bp_wind_speed",
        "source_unit": RPM_UNIT,
        "access": PointAccess.READ,
    }
    values.update(overrides)
    return Point(**values)


# --------------------------------------------------------------------------
# BusinessPoint
# --------------------------------------------------------------------------


def test_business_point_id_trimmed_and_blank_rejected() -> None:
    point = BusinessPoint(
        business_point_id="  bp1  ", data_type=DataType.FLOAT32, standard_unit=RPM_UNIT
    )
    assert point.business_point_id == "bp1"
    with pytest.raises(ValueError, match="business_point_id must not be empty"):
        BusinessPoint(business_point_id="   ", data_type=DataType.FLOAT32, standard_unit=RPM_UNIT)


def test_business_point_coerce_delegates_to_data_type() -> None:
    point = BusinessPoint(
        business_point_id="bp1", data_type=DataType.FLOAT32, standard_unit=RPM_UNIT
    )
    assert point.coerce(12) == pytest.approx(12.0)


# --------------------------------------------------------------------------
# Point
# --------------------------------------------------------------------------


def test_point_ids_trimmed_and_blank_rejected() -> None:
    point = _point(point_id="  p1  ")
    assert point.point_id == "p1"
    with pytest.raises(ValueError, match="point_id must not be empty"):
        _point(point_id=" ")
    with pytest.raises(ValueError, match="business_point_id must not be empty"):
        _point(business_point_id=" ")


@pytest.mark.parametrize(
    ("scale", "offset"),
    [(0.0, 0.0), (math.nan, 0.0), (1.0, math.inf)],
)
def test_point_rejects_bad_scale_and_offset(scale: float, offset: float) -> None:
    with pytest.raises(ValueError):
        _point(scale=scale, offset=offset)


def test_point_ext_frozen_and_scalar_only() -> None:
    point = _point(ext={"address": 1, "ratio": 1.5, "flag": True, "missing": None})
    assert isinstance(point.ext, MappingProxyType)
    with pytest.raises(ValueError, match="non-empty strings"):
        _point(ext={"  ": 1})
    with pytest.raises(ValueError, match="scalar value or null"):
        _point(ext={"bad": [1]})
    with pytest.raises(ValueError, match="must be finite"):
        _point(ext={"ratio": math.nan})


def test_point_groups_validated() -> None:
    point = _point(point_groups=("fast", "slow"))
    assert point.point_groups == ("fast", "slow")
    with pytest.raises(ValueError, match="non-empty names"):
        _point(point_groups=("  ",))
    with pytest.raises(ValueError, match="duplicates"):
        _point(point_groups=("a", "a"))


# --------------------------------------------------------------------------
# 工程值 / 写值逆变换
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, True, "open"])
def test_engineering_value_passthrough(raw) -> None:
    point = _point(scale=2.0, offset=1.0)
    assert engineering_value(point, raw) is raw


def test_engineering_value_identity_transform() -> None:
    point = _point()
    assert engineering_value(point, 42) == 42


def test_engineering_value_applies_scale_offset() -> None:
    point = _point(scale=2.0, offset=1.0)
    assert engineering_value(point, 5) == 11.0


@pytest.mark.parametrize("value", [True, "reset"])
def test_raw_write_value_passthrough(value) -> None:
    point = _point(scale=2.0, offset=1.0)
    assert raw_write_value(point, value) is value


def test_raw_write_value_inverse_transform() -> None:
    point = _point(scale=2.0, offset=1.0)
    assert raw_write_value(point, 11.0) == pytest.approx(5.0)


# --------------------------------------------------------------------------
# PointTable
# --------------------------------------------------------------------------


def _table(**overrides) -> PointTable:
    values = {
        "point_table_id": "table-1",
        "protocol": Protocol(name="modbus"),
        "points": {"p1": _point(point_id="p1")},
    }
    values.update(overrides)
    return PointTable(**values)


def test_point_table_id_validated() -> None:
    with pytest.raises(ValueError, match="point_table_id must not be empty"):
        _table(point_table_id="  ")


def test_point_table_parent_id_constraints() -> None:
    with pytest.raises(ValueError, match="non-empty and different"):
        _table(parent_id="  ")
    with pytest.raises(ValueError, match="non-empty and different"):
        _table(parent_id="table-1")
    table = _table(parent_id=" base ")
    assert table.parent_id == "base"


def test_point_table_keys_must_match_points() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _table(points={"": _point(point_id="p1")})
    with pytest.raises(ValueError, match="surrounding whitespace"):
        _table(points={" p1": _point(point_id="p1")})
    with pytest.raises(ValueError, match="does not match"):
        _table(points={"other": _point(point_id="p1")})


def test_point_table_points_frozen_and_lookups() -> None:
    p1 = _point(point_id="p1", business_point_id="bp1")
    p2 = _point(point_id="p2", business_point_id="bp1")
    table = _table(points={"p1": p1, "p2": p2})

    assert isinstance(table.points, MappingProxyType)
    assert table.point("  p2  ") is p2
    assert {point.point_id for point in table.points_for_business("bp1")} == {"p1", "p2"}
    assert table.points_for_business("missing") == ()
