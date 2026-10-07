"""工程量类别。"""

from __future__ import annotations

from enum import StrEnum


class Quantity(StrEnum):
    """单位换算使用的稳定工程量语义。

    这里表达业务量类别，而不是纯物理量纲。例如有功、无功和视在功率
    具有相同物理量纲，但在业务语义上不可互换，因此分别建模。
    """

    DIMENSIONLESS = "dimensionless"
    VOLTAGE = "voltage"
    CURRENT = "current"
    ACTIVE_POWER = "active_power"
    REACTIVE_POWER = "reactive_power"
    APPARENT_POWER = "apparent_power"
    FREQUENCY = "frequency"
    ENERGY = "energy"
    LENGTH = "length"
    SPEED = "speed"
    ACCELERATION = "acceleration"
    ANGLE = "angle"
    ANGULAR_SPEED = "angular_speed"
    ROTATIONAL_SPEED = "rotational_speed"
    ROTATIONAL_ACCELERATION = "rotational_acceleration"
    FORCE = "force"
    TORQUE = "torque"
    PRESSURE = "pressure"
    TEMPERATURE = "temperature"
    TIME = "time"
    RESISTANCE = "resistance"
    CONDUCTANCE = "conductance"
    CAPACITANCE = "capacitance"
    INDUCTANCE = "inductance"
    FREQUENCY_RATE = "frequency_rate"
