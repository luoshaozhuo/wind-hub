from __future__ import annotations

from math import pi

import pytest

from core.domain import UNIT_CATALOG, UnitCode, convert_value


def test_rpm_converts_to_radians_per_second() -> None:
    value = convert_value(
        60.0,
        UNIT_CATALOG[UnitCode.RPM],
        UNIT_CATALOG[UnitCode.RADIAN_PER_SECOND],
    )

    assert value == pytest.approx(2.0 * pi)
