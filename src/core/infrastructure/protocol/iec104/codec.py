"""IEC104 c104 值、品质、时间戳与控制命令转换。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

from core.application.errors import ConfigError
from core.application.protocol_contract import ProtocolSample, Quality
from .mapping import IEC104Point


def sample_from_c104(point: Any, point_id: str) -> ProtocolSample:
    """把 c104 Point 当前状态转换为共享 ProtocolSample。"""
    return ProtocolSample(
        point_id=point_id,
        value=_value_from_c104(getattr(point, "value", None)),
        quality=_quality_from_c104(getattr(point, "quality", None)),
        timestamp=_timestamp_from_c104(getattr(point, "recorded_at", None)),
    )


def command_to_c104(
    mapped: IEC104Point,
    value: object,
) -> tuple[Any, object]:
    """按点表 type_id 把协议侧值转换成 c104 控制类型和值。"""
    c104 = _c104()
    type_id = mapped.type_id
    if type_id is None:
        raise ConfigError(
            f"IEC104 writable point '{mapped.point_id}' requires type_id"
        )

    if type_id == "C_SC_NA_1":
        if type(value) is not bool:
            raise ValueError("C_SC_NA_1 requires boolean value")
        return c104.Type.C_SC_NA_1, value

    if type_id == "C_DC_NA_1":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("C_DC_NA_1 requires double-point value 1 or 2")
        integer = int(value)
        if float(value) != float(integer) or integer not in (1, 2):
            raise ValueError("C_DC_NA_1 requires 1=OFF or 2=ON")
        state = c104.Double.OFF if integer == 1 else c104.Double.ON
        return c104.Type.C_DC_NA_1, state

    if type_id == "C_SE_NA_1":
        number = _numeric(value, type_id)
        if not -1.0 <= number <= 1.0:
            raise ValueError("C_SE_NA_1 normalized value must be in -1..1")
        return c104.Type.C_SE_NA_1, c104.NormalizedFloat(number)

    if type_id == "C_SE_NB_1":
        number = _integer(value, type_id)
        if not -32768 <= number <= 32767:
            raise ValueError("C_SE_NB_1 scaled value must be in int16 range")
        return c104.Type.C_SE_NB_1, c104.Int16(number)

    if type_id == "C_SE_NC_1":
        return c104.Type.C_SE_NC_1, _numeric(value, type_id)

    raise ConfigError(
        f"IEC104 point '{mapped.point_id}' uses unsupported command type "
        f"'{type_id}'"
    )


def _quality_from_c104(quality: Any) -> Quality:
    if quality is None:
        return Quality.GOOD

    c104 = _c104()
    bits = getattr(quality, "value", 0)
    invalid = getattr(c104.Quality.Invalid, "value", 0)
    uncertain = (
        getattr(c104.Quality.NonTopical, "value", 0)
        | getattr(c104.Quality.Substituted, "value", 0)
        | getattr(c104.Quality.Blocked, "value", 0)
        | getattr(c104.Quality.Overflow, "value", 0)
    )
    if bits & invalid:
        return Quality.BAD
    if bits & uncertain:
        return Quality.UNCERTAIN
    return Quality.GOOD


def _timestamp_from_c104(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("c104 recorded_at must be datetime or None")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _value_from_c104(value: object) -> float | int | bool | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, IntEnum):
        return int(value)
    if isinstance(value, str | int | float):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric(value: object, type_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{type_id} requires numeric value")
    return float(value)


def _integer(value: object, type_id: str) -> int:
    number = _numeric(value, type_id)
    integer = int(number)
    if number != float(integer):
        raise ValueError(f"{type_id} requires integer value")
    return integer


def _c104() -> Any:
    try:
        import c104
    except ImportError as exc:
        raise ConfigError(
            "IEC104 support requires the optional 'c104' dependency"
        ) from exc
    return c104
