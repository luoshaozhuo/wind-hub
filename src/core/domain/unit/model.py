"""工程单位值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from .quantity import Quantity


class UnitCode(StrEnum):
    """系统内置工程单位的稳定代码。"""

    NONE = "none"
    PERCENT = "percent"

    VOLT = "volt"
    KILOVOLT = "kilovolt"
    AMPERE = "ampere"

    WATT = "watt"
    KILOWATT = "kilowatt"
    MEGAWATT = "megawatt"
    VAR = "var"
    KILOVAR = "kilovar"
    MEGAVAR = "megavar"
    VOLT_AMPERE = "volt_ampere"
    KILOVOLT_AMPERE = "kilovolt_ampere"
    MEGAVOLT_AMPERE = "megavolt_ampere"

    HERTZ = "hertz"
    HERTZ_PER_SECOND = "hertz_per_second"

    WATT_HOUR = "watt_hour"
    KILOWATT_HOUR = "kilowatt_hour"
    MEGAWATT_HOUR = "megawatt_hour"

    METER = "meter"
    MILLIMETER = "millimeter"
    METER_PER_SECOND = "meter_per_second"
    METER_PER_SECOND_SQUARED = "meter_per_second_squared"
    STANDARD_GRAVITY = "standard_gravity"

    RADIAN = "radian"
    DEGREE = "degree"
    RADIAN_PER_SECOND = "radian_per_second"
    DEGREE_PER_SECOND = "degree_per_second"
    RPM = "rpm"
    RPM_PER_SECOND = "rpm_per_second"

    NEWTON = "newton"
    KILONEWTON = "kilonewton"
    NEWTON_METER = "newton_meter"
    KILONEWTON_METER = "kilonewton_meter"

    PASCAL = "pascal"
    KILOPASCAL = "kilopascal"
    MEGAPASCAL = "megapascal"
    BAR = "bar"

    KELVIN = "kelvin"
    CELSIUS = "celsius"

    SECOND = "second"
    MILLISECOND = "millisecond"
    MINUTE = "minute"
    HOUR = "hour"

    OHM = "ohm"
    SIEMENS = "siemens"
    FARAD = "farad"
    HENRY = "henry"


@dataclass(frozen=True, slots=True)
class Unit:
    """不可变工程单位值对象。

    scale_to_base 与 offset_to_base 将单位值映射到所属 Quantity 的内部基准单位：

    base_value = value * scale_to_base + offset_to_base

    Unit 没有独立生命周期和实体身份；相同字段表示相同单位值。
    """

    code: UnitCode
    symbol: str
    quantity: Quantity
    scale_to_base: float = 1.0
    offset_to_base: float = 0.0
    name: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip()
        name = self.name.strip() if self.name is not None else None

        if not isfinite(self.scale_to_base) or self.scale_to_base <= 0.0:
            raise ValueError("scale_to_base must be finite and greater than zero")
        if not isfinite(self.offset_to_base):
            raise ValueError("offset_to_base must be finite")
        if name == "":
            name = None

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "name", name)
