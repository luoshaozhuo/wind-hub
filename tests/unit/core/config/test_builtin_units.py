"""内置单位解析测试：配置读取无需 units.yaml。"""

import pytest

from core.domain.unit import KILOWATT, UNIT_CATALOG, UnitCode


def test_builtin_unit_without_units_yaml() -> None:
    assert UNIT_CATALOG[UnitCode("kilowatt")] is KILOWATT


def test_unknown_unit_rejected() -> None:
    with pytest.raises(ValueError):
        UnitCode("not-a-unit")
