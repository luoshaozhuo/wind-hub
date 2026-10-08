"""系统内置工程单位目录。"""

from __future__ import annotations

from math import pi
from types import MappingProxyType

from .model import Unit, UnitCode
from .quantity import Quantity

NONE = Unit(UnitCode.NONE, "", Quantity.DIMENSIONLESS)
PERCENT = Unit(
    UnitCode.PERCENT,
    "%",
    Quantity.DIMENSIONLESS,
    scale_to_base=0.01,
)

VOLT = Unit(UnitCode.VOLT, "V", Quantity.VOLTAGE)
KILOVOLT = Unit(
    UnitCode.KILOVOLT,
    "kV",
    Quantity.VOLTAGE,
    scale_to_base=1_000.0,
)
AMPERE = Unit(UnitCode.AMPERE, "A", Quantity.CURRENT)

WATT = Unit(UnitCode.WATT, "W", Quantity.ACTIVE_POWER)
KILOWATT = Unit(
    UnitCode.KILOWATT,
    "kW",
    Quantity.ACTIVE_POWER,
    scale_to_base=1_000.0,
)
MEGAWATT = Unit(
    UnitCode.MEGAWATT,
    "MW",
    Quantity.ACTIVE_POWER,
    scale_to_base=1_000_000.0,
)
VAR = Unit(UnitCode.VAR, "var", Quantity.REACTIVE_POWER)
KILOVAR = Unit(
    UnitCode.KILOVAR,
    "kvar",
    Quantity.REACTIVE_POWER,
    scale_to_base=1_000.0,
)
MEGAVAR = Unit(
    UnitCode.MEGAVAR,
    "Mvar",
    Quantity.REACTIVE_POWER,
    scale_to_base=1_000_000.0,
)
VOLT_AMPERE = Unit(
    UnitCode.VOLT_AMPERE,
    "VA",
    Quantity.APPARENT_POWER,
)
KILOVOLT_AMPERE = Unit(
    UnitCode.KILOVOLT_AMPERE,
    "kVA",
    Quantity.APPARENT_POWER,
    scale_to_base=1_000.0,
)
MEGAVOLT_AMPERE = Unit(
    UnitCode.MEGAVOLT_AMPERE,
    "MVA",
    Quantity.APPARENT_POWER,
    scale_to_base=1_000_000.0,
)

HERTZ = Unit(UnitCode.HERTZ, "Hz", Quantity.FREQUENCY)
HERTZ_PER_SECOND = Unit(
    UnitCode.HERTZ_PER_SECOND,
    "Hz/s",
    Quantity.FREQUENCY_RATE,
)

WATT_HOUR = Unit(UnitCode.WATT_HOUR, "Wh", Quantity.ENERGY)
KILOWATT_HOUR = Unit(
    UnitCode.KILOWATT_HOUR,
    "kWh",
    Quantity.ENERGY,
    scale_to_base=1_000.0,
)
MEGAWATT_HOUR = Unit(
    UnitCode.MEGAWATT_HOUR,
    "MWh",
    Quantity.ENERGY,
    scale_to_base=1_000_000.0,
)

METER = Unit(UnitCode.METER, "m", Quantity.LENGTH)
MILLIMETER = Unit(
    UnitCode.MILLIMETER,
    "mm",
    Quantity.LENGTH,
    scale_to_base=0.001,
)
METER_PER_SECOND = Unit(
    UnitCode.METER_PER_SECOND,
    "m/s",
    Quantity.SPEED,
)
METER_PER_SECOND_SQUARED = Unit(
    UnitCode.METER_PER_SECOND_SQUARED,
    "m/s²",
    Quantity.ACCELERATION,
)
STANDARD_GRAVITY = Unit(
    UnitCode.STANDARD_GRAVITY,
    "g",
    Quantity.ACCELERATION,
    scale_to_base=9.80665,
)

RADIAN = Unit(UnitCode.RADIAN, "rad", Quantity.ANGLE)
DEGREE = Unit(
    UnitCode.DEGREE,
    "°",
    Quantity.ANGLE,
    scale_to_base=pi / 180.0,
)
RADIAN_PER_SECOND = Unit(
    UnitCode.RADIAN_PER_SECOND,
    "rad/s",
    Quantity.ANGULAR_SPEED,
)
DEGREE_PER_SECOND = Unit(
    UnitCode.DEGREE_PER_SECOND,
    "°/s",
    Quantity.ANGULAR_SPEED,
    scale_to_base=pi / 180.0,
)
RPM = Unit(
    UnitCode.RPM,
    "r/min",
    Quantity.ANGULAR_SPEED,
    scale_to_base=2.0 * pi / 60.0,
)
RADIAN_PER_SECOND_SQUARED = Unit(
    UnitCode.RADIAN_PER_SECOND_SQUARED,
    "rad/s²",
    Quantity.ANGULAR_ACCELERATION,
)
DEGREE_PER_SECOND_SQUARED = Unit(
    UnitCode.DEGREE_PER_SECOND_SQUARED,
    "°/s²",
    Quantity.ANGULAR_ACCELERATION,
    scale_to_base=pi / 180.0,
)
RPM_PER_SECOND = Unit(
    UnitCode.RPM_PER_SECOND,
    "r/min/s",
    Quantity.ANGULAR_ACCELERATION,
    scale_to_base=2.0 * pi / 60.0,
)

NEWTON = Unit(UnitCode.NEWTON, "N", Quantity.FORCE)
KILONEWTON = Unit(
    UnitCode.KILONEWTON,
    "kN",
    Quantity.FORCE,
    scale_to_base=1_000.0,
)
NEWTON_METER = Unit(
    UnitCode.NEWTON_METER,
    "N·m",
    Quantity.TORQUE,
)
KILONEWTON_METER = Unit(
    UnitCode.KILONEWTON_METER,
    "kN·m",
    Quantity.TORQUE,
    scale_to_base=1_000.0,
)

PASCAL = Unit(UnitCode.PASCAL, "Pa", Quantity.PRESSURE)
KILOPASCAL = Unit(
    UnitCode.KILOPASCAL,
    "kPa",
    Quantity.PRESSURE,
    scale_to_base=1_000.0,
)
MEGAPASCAL = Unit(
    UnitCode.MEGAPASCAL,
    "MPa",
    Quantity.PRESSURE,
    scale_to_base=1_000_000.0,
)
BAR = Unit(
    UnitCode.BAR,
    "bar",
    Quantity.PRESSURE,
    scale_to_base=100_000.0,
)

KELVIN = Unit(UnitCode.KELVIN, "K", Quantity.TEMPERATURE)
CELSIUS = Unit(
    UnitCode.CELSIUS,
    "°C",
    Quantity.TEMPERATURE,
    offset_to_base=273.15,
)

SECOND = Unit(UnitCode.SECOND, "s", Quantity.TIME)
MILLISECOND = Unit(
    UnitCode.MILLISECOND,
    "ms",
    Quantity.TIME,
    scale_to_base=0.001,
)
MINUTE = Unit(
    UnitCode.MINUTE,
    "min",
    Quantity.TIME,
    scale_to_base=60.0,
)
HOUR = Unit(
    UnitCode.HOUR,
    "h",
    Quantity.TIME,
    scale_to_base=3_600.0,
)

OHM = Unit(UnitCode.OHM, "Ω", Quantity.RESISTANCE)
SIEMENS = Unit(UnitCode.SIEMENS, "S", Quantity.CONDUCTANCE)
FARAD = Unit(UnitCode.FARAD, "F", Quantity.CAPACITANCE)
HENRY = Unit(UnitCode.HENRY, "H", Quantity.INDUCTANCE)

_ALL_UNITS = (
    NONE,
    PERCENT,
    VOLT,
    KILOVOLT,
    AMPERE,
    WATT,
    KILOWATT,
    MEGAWATT,
    VAR,
    KILOVAR,
    MEGAVAR,
    VOLT_AMPERE,
    KILOVOLT_AMPERE,
    MEGAVOLT_AMPERE,
    HERTZ,
    HERTZ_PER_SECOND,
    WATT_HOUR,
    KILOWATT_HOUR,
    MEGAWATT_HOUR,
    METER,
    MILLIMETER,
    METER_PER_SECOND,
    METER_PER_SECOND_SQUARED,
    STANDARD_GRAVITY,
    RADIAN,
    DEGREE,
    RADIAN_PER_SECOND,
    DEGREE_PER_SECOND,
    RPM,
    RADIAN_PER_SECOND_SQUARED,
    DEGREE_PER_SECOND_SQUARED,
    RPM_PER_SECOND,
    NEWTON,
    KILONEWTON,
    NEWTON_METER,
    KILONEWTON_METER,
    PASCAL,
    KILOPASCAL,
    MEGAPASCAL,
    BAR,
    KELVIN,
    CELSIUS,
    SECOND,
    MILLISECOND,
    MINUTE,
    HOUR,
    OHM,
    SIEMENS,
    FARAD,
    HENRY,
)

UNIT_CATALOG = MappingProxyType({unit.code: unit for unit in _ALL_UNITS})

if len(UNIT_CATALOG) != len(_ALL_UNITS):
    raise RuntimeError("duplicate unit code in built-in unit catalog")
