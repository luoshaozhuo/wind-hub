"""内置标准单位解析：无需 units.yaml。"""

import pytest

from core.application import ConfigError
from core.domain.config import PointConfig
from core.domain.unit import KILOWATT
from core.infrastructure.config.assembly import _resolve_unit


def point(unit: str) -> PointConfig:
    return PointConfig(
        point_id="power",
        variable_name=None,
        point_groups=("default",),
        address={"register_type": "holding", "address": 0},
        data_type="float32",
        scale=1.0,
        offset=0.0,
        unit=unit,
        description=None,
    )


def test_builtin_unit_without_units_yaml() -> None:
    assert _resolve_unit(point(KILOWATT.code.value)) is KILOWATT


def test_unknown_unit_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown built-in unit"):
        _resolve_unit(point("not-a-unit"))
