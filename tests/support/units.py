"""测试共享的工程单位辅助：最小单位目录与常用 Unit 常量。"""

from __future__ import annotations

from typing import NotRequired, TypedDict

from core.domain.unit import DIMENSIONLESS, Quantity, Unit, UnitCatalog

NONE_UNIT = Unit(unit_id="none", symbol="", quantity=DIMENSIONLESS)
WATT_UNIT = Unit(unit_id="watt", symbol="W", quantity=Quantity("active_power"))
KILOWATT_UNIT = Unit(
    unit_id="kilowatt",
    symbol="kW",
    quantity=Quantity("active_power"),
    scale_to_base=1000.0,
)
RADIAN_PER_SECOND_UNIT = Unit(
    unit_id="radian_per_second",
    symbol="rad/s",
    quantity=Quantity("angular_speed"),
)
DEGREE_UNIT = Unit(
    unit_id="degree",
    symbol="°",
    quantity=Quantity("angle"),
    scale_to_base=0.017453292519943295,
)
RPM_UNIT = Unit(
    unit_id="rpm",
    symbol="r/min",
    quantity=Quantity("angular_speed"),
    scale_to_base=0.10471975511965977,
)
RADIAN_PER_SECOND_SQUARED_UNIT = Unit(
    unit_id="radian_per_second_squared",
    symbol="rad/s²",
    quantity=Quantity("angular_acceleration"),
)
RPM_PER_SECOND_UNIT = Unit(
    unit_id="rpm_per_second",
    symbol="r/min/s",
    quantity=Quantity("angular_acceleration"),
    scale_to_base=0.10471975511965977,
)
CELSIUS_UNIT = Unit(
    unit_id="celsius",
    symbol="°C",
    quantity=Quantity("temperature"),
    offset_to_base=273.15,
)
KELVIN_UNIT = Unit(unit_id="kelvin", symbol="K", quantity=Quantity("temperature"))
PERCENT_UNIT = Unit(
    unit_id="percent",
    symbol="%",
    quantity=DIMENSIONLESS,
    scale_to_base=0.01,
)


class QuantityDefinition(TypedDict):
    base_unit: str


class UnitDefinition(TypedDict):
    symbol: str
    quantity: str
    scale_to_base: NotRequired[float]
    offset_to_base: NotRequired[float]


class UnitsDocument(TypedDict):
    quantities: dict[str, QuantityDefinition]
    units: dict[str, UnitDefinition]


#: 最小 units.yaml 文档：覆盖测试引用的全部 Unit ID。
MINIMAL_UNITS_DOCUMENT: UnitsDocument = {
    "quantities": {
        "dimensionless": {"base_unit": "none"},
        "active_power": {"base_unit": "watt"},
        "reactive_power": {"base_unit": "var"},
        "voltage": {"base_unit": "volt"},
        "current": {"base_unit": "ampere"},
        "frequency": {"base_unit": "hertz"},
        "energy": {"base_unit": "watt_hour"},
        "speed": {"base_unit": "meter_per_second"},
        "angle": {"base_unit": "radian"},
        "angular_speed": {"base_unit": "radian_per_second"},
        "temperature": {"base_unit": "kelvin"},
        "pressure": {"base_unit": "pascal"},
        "time": {"base_unit": "second"},
        "length": {"base_unit": "meter"},
    },
    "units": {
        "none": {"symbol": "", "quantity": "dimensionless"},
        "percent": {"symbol": "%", "quantity": "dimensionless", "scale_to_base": 0.01},
        "watt": {"symbol": "W", "quantity": "active_power"},
        "kilowatt": {"symbol": "kW", "quantity": "active_power", "scale_to_base": 1000.0},
        "megawatt": {"symbol": "MW", "quantity": "active_power", "scale_to_base": 1000000.0},
        "var": {"symbol": "var", "quantity": "reactive_power"},
        "kilovar": {"symbol": "kvar", "quantity": "reactive_power", "scale_to_base": 1000.0},
        "volt": {"symbol": "V", "quantity": "voltage"},
        "ampere": {"symbol": "A", "quantity": "current"},
        "hertz": {"symbol": "Hz", "quantity": "frequency"},
        "watt_hour": {"symbol": "Wh", "quantity": "energy"},
        "kilowatt_hour": {"symbol": "kWh", "quantity": "energy", "scale_to_base": 1000.0},
        "meter_per_second": {"symbol": "m/s", "quantity": "speed"},
        "radian": {"symbol": "rad", "quantity": "angle"},
        "degree": {
            "symbol": "°",
            "quantity": "angle",
            "scale_to_base": 0.017453292519943295,
        },
        "radian_per_second": {"symbol": "rad/s", "quantity": "angular_speed"},
        "rpm": {
            "symbol": "r/min",
            "quantity": "angular_speed",
            "scale_to_base": 0.10471975511965977,
        },
        "kelvin": {"symbol": "K", "quantity": "temperature"},
        "celsius": {"symbol": "°C", "quantity": "temperature", "offset_to_base": 273.15},
        "pascal": {"symbol": "Pa", "quantity": "pressure"},
        "bar": {"symbol": "bar", "quantity": "pressure", "scale_to_base": 100000.0},
        "second": {"symbol": "s", "quantity": "time"},
        "millisecond": {"symbol": "ms", "quantity": "time", "scale_to_base": 0.001},
        "meter": {"symbol": "m", "quantity": "length"},
    },
}


def minimal_unit_catalog() -> UnitCatalog:
    """由 MINIMAL_UNITS_DOCUMENT 构造 UnitCatalog（不经 YAML）。"""
    quantities = {
        quantity_id: definition["base_unit"]
        for quantity_id, definition in MINIMAL_UNITS_DOCUMENT["quantities"].items()
    }
    units: list[Unit] = []
    for unit_id, definition in MINIMAL_UNITS_DOCUMENT["units"].items():
        units.append(
            Unit(
                unit_id=unit_id,
                symbol=definition["symbol"],
                quantity=Quantity(definition["quantity"]),
                scale_to_base=definition.get("scale_to_base", 1.0),
                offset_to_base=definition.get("offset_to_base", 0.0),
            )
        )
    return UnitCatalog.build(quantities=quantities, units=units)
