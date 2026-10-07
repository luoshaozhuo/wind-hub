"""系统内置工程单位目录。"""

from __future__ import annotations

from math import pi
from types import MappingProxyType

from .model import Unit, UnitCode
from .quantity import Quantity


NONE = Unit(UnitCode.NONE, "", Quantity.DIMENSIONLESS, name="dimensionless")
PERCENT = Unit(
    UnitCode.PERCENT,
    "%",
    Quantity.DIMENSIONLESS,
    scale_to_base=0.01,
    name="percent",
)

VOLT = Unit(UnitCode.VOLT, "V", Quantity.VOLTAGE, name="volt")
KILOVOLT = Unit(
    UnitCode.KILOVOLT,
    "kV",
    Quantity.VOLTAGE,
    scale_to_base=1_000.0,
    name="kilovolt",
)
AMPERE = Unit(UnitCode.AMPERE, "A", Quantity.CURRENT, name="ampere")

WATT = Unit(UnitCode.WATT, "W", Quantity.ACTIVE_POWER, name="watt")
KILOWATT = Unit(
    UnitCode.KILOWATT,
    "kW",
    Quantity.ACTIVE_POWER,
    scale_to_base=1_000.0,
    name="kilowatt",
)
MEGAWATT = Unit(
    UnitCode.MEGAWATT,
    "MW",
    Quantity.ACTIVE_POWER,
    scale_to_base=1_000_000.0,
    name="megawatt",
)
VAR = Unit(UnitCode.VAR, "var", Quantity.REACTIVE_POWER, name="var")
KILOVAR = Unit(
    UnitCode.KILOVAR,
    "kvar",
    Quantity.REACTIVE_POWER,
    scale_to_base=1_000.0,
    name="kilovar",
)
MEGAVAR = Unit(
    UnitCode.MEGAVAR,
    "Mvar",
    Quantity.REACTIVE_POWER,
    scale_to_base=1_000_000.0,
    name="megavar",
)
VOLT_AMPERE = Unit(
    UnitCode.VOLT_AMPERE,
    "VA",
    Quantity.APPARENT_POWER,
    name="volt-ampere",
)
KILOVOLT_AMPERE = Unit(
    UnitCode.KILOVOLT_AMPERE,
    "kVA",
    Quantity.APPARENT_POWER,
    scale_to_base=1_000.0,
    name="kilovolt-ampere",
)
MEGAVOLT_AMPERE = Unit(
    UnitCode.MEGAVOLT_AMPERE,
    "MVA",
    Quantity.APPARENT_POWER,
    scale_to_base=1_000_000.0,
    name="megavolt-ampere",
)

HERTZ = Unit(UnitCode.HERTZ, "Hz", Quantity.FREQUENCY, name="hertz")
HERTZ_PER_SECOND = Unit(
    UnitCode.HERTZ_PER_SECOND,
    "Hz/s",
    Quantity.FREQUENCY_RATE,
    name="hertz per second",
)

WATT_HOUR = Unit(UnitCode.WATT_HOUR, "Wh", Quantity.ENERGY, name="watt-hour")
KILOWATT_HOUR = Unit(
    UnitCode.KILOWATT_HOUR,
    "kWh",
    Quantity.ENERGY,
    scale_to_base=1_000.0,
    name="kilowatt-hour",
)
MEGAWATT_HOUR = Unit(
    UnitCode.MEGAWATT_HOUR,
    "MWh",
    Quantity.ENERGY,
    scale_to_base=1_000_000.0,
    name="megawatt-hour",
)

METER = Unit(UnitCode.METER, "m", Quantity.LENGTH, name="meter")
MILLIMETER = Unit(
    UnitCode.MILLIMETER,
    "mm",
    Quantity.LENGTH,
    scale_to_base=0.001,
    name="millimeter",
)
METER_PER_SECOND = Unit(
    UnitCode.METER_PER_SECOND,
    "m/s",
    Quantity.SPEED,
    name="meter per second",
)
METER_PER_SECOND_SQUARED = Unit(
    UnitCode.METER_PER_SECOND_SQUARED,
    "m/s²",
    Quantity.ACCELERATION,
    name="meter per second squared",
)
STANDARD_GRAVITY = Unit(
    UnitCode.STANDARD_GRAVITY,
    "g",
    Quantity.ACCELERATION,
    scale_to_base=9.80665,
    name="standard gravity",
)

RADIAN = Unit(UnitCode.RADIAN, "rad", Quantity.ANGLE, name="radian")
DEGREE = Unit(
    UnitCode.DEGREE,
    "°",
    Quantity.ANGLE,
    scale_to_base=pi / 180.0,
    name="degree",
)
RADIAN_PER_SECOND = Unit(
    UnitCode.RADIAN_PER_SECOND,
    "rad/s",
    Quantity.ANGULAR_SPEED,
    name="radian per second",
)
DEGREE_PER_SECOND = Unit(
    UnitCode.DEGREE_PER_SECOND,
    "°/s",
    Quantity.ANGULAR_SPEED,
    scale_to_base=pi / 180.0,
    name="degree per second",
)
RPM = Unit(
    UnitCode.RPM,
    "r/min",
    Quantity.ROTATIONAL_SPEED,
    scale_to_base=2.0 * pi / 60.0,
    name="revolution per minute",
)
RPM_PER_SECOND = Unit(
    UnitCode.RPM_PER_SECOND,
    "r/min/s",
    Quantity.ROTATIONAL_ACCELERATION,
    scale_to_base=2.0 * pi / 60.0,
    name="revolution per minute per second",
)

NEWTON = Unit(UnitCode.NEWTON, "N", Quantity.FORCE, name="newton")
KILONEWTON = Unit(
    UnitCode.KILONEWTON,
    "kN",
    Quantity.FORCE,
    scale_to_base=1_000.0,
    name="kilonewton",
)
NEWTON_METER = Unit(
    UnitCode.NEWTON_METER,
    "N·m",
    Quantity.TORQUE,
    name="newton-meter",
)
KILONEWTON_METER = Unit(
    UnitCode.KILONEWTON_METER,
    "kN·m",
    Quantity.TORQUE,
    scale_to_base=1_000.0,
    name="kilonewton-meter",
)

PASCAL = Unit(UnitCode.PASCAL, "Pa", Quantity.PRESSURE, name="pascal")
KILOPASCAL = Unit(
    UnitCode.KILOPASCAL,
    "kPa",
    Quantity.PRESSURE,
    scale_to_base=1_000.0,
    name="kilopascal",
)
MEGAPASCAL = Unit(
    UnitCode.MEGAPASCAL,
    "MPa",
    Quantity.PRESSURE,
    scale_to_base=1_000_000.0,
    name="megapascal",
)
BAR = Unit(
    UnitCode.BAR,
    "bar",
    Quantity.PRESSURE,
    scale_to_base=100_000.0,
    name="bar",
)

KELVIN = Unit(UnitCode.KELVIN, "K", Quantity.TEMPERATURE, name="kelvin")
CELSIUS = Unit(
    UnitCode.CELSIUS,
    "°C",
    Quantity.TEMPERATURE,
    offset_to_base=273.15,
    name="degree Celsius",
)

SECOND = Unit(UnitCode.SECOND, "s", Quantity.TIME, name="second")
MILLISECOND = Unit(
    UnitCode.MILLISECOND,
    "ms",
    Quantity.TIME,
    scale_to_base=0.001,
    name="millisecond",
)
MINUTE = Unit(
    UnitCode.MINUTE,
    "min",
    Quantity.TIME,
    scale_to_base=60.0,
    name="minute",
)
HOUR = Unit(
    UnitCode.HOUR,
    "h",
    Quantity.TIME,
    scale_to_base=3_600.0,
    name="hour",
)

OHM = Unit(UnitCode.OHM, "Ω", Quantity.RESISTANCE, name="ohm")
SIEMENS = Unit(UnitCode.SIEMENS, "S", Quantity.CONDUCTANCE, name="siemens")
FARAD = Unit(UnitCode.FARAD, "F", Quantity.CAPACITANCE, name="farad")
HENRY = Unit(UnitCode.HENRY, "H", Quantity.INDUCTANCE, name="henry")

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
