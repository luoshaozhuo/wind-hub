from __future__ import annotations

from math import pi

import pytest

from core.domain import convert_value
from tests.support.units import (
    RADIAN_PER_SECOND_SQUARED_UNIT,
    RADIAN_PER_SECOND_UNIT,
    RPM_PER_SECOND_UNIT,
    RPM_UNIT,
)


def test_rpm_converts_to_radians_per_second() -> None:
    value = convert_value(
        60.0,
        RPM_UNIT,
        RADIAN_PER_SECOND_UNIT,
    )

    assert value == pytest.approx(2.0 * pi)


def test_rpm_per_second_converts_to_radians_per_second_squared() -> None:
    value = convert_value(
        1.0,
        RPM_PER_SECOND_UNIT,
        RADIAN_PER_SECOND_SQUARED_UNIT,
    )

    assert value == pytest.approx(2.0 * pi / 60.0)
