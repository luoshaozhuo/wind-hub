"""Modbus ProtocolPoint 地址解析与批量读取分组。"""

from __future__ import annotations

from dataclasses import dataclass

from core.application import ConfigError
from core.domain import PointAccess, ProtocolPoint

_REGISTER_TYPE_ALIASES: dict[str, str] = {
    "coil": "coil",
    "discrete_input": "discrete_input",
    "discrete": "discrete_input",
    "input": "input",
    "input_register": "input",
    "holding": "holding",
    "holding_register": "holding",
}
_VALID_WORD_ORDERS = frozenset({"big_endian", "little_endian"})
_READ_ONLY_TYPES = frozenset({"discrete_input", "input"})
_ALLOWED_POINT_OPTIONS = frozenset(
    {"register_type", "type", "address", "count", "word_order"}
)
_REGISTER_COUNTS: dict[str, int] = {
    "bool": 1,
    "int8": 1,
    "uint8": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}


@dataclass(frozen=True, slots=True)
class ModbusPoint:
    """规范化 Modbus 点地址。"""

    point_id: str
    register_type: str
    address: int
    count: int
    data_type: str
    word_order: str


def parse_modbus_point(
    point: ProtocolPoint,
    *,
    default_word_order: str,
) -> ModbusPoint:
    """把 ProtocolPoint 解析成 ModbusPoint，并尽早校验配置。"""
    options = point.protocol_options
    unknown = set(options) - _ALLOWED_POINT_OPTIONS
    if unknown:
        raise ConfigError(
            f"Modbus point '{point.point_id}' has unknown options: {sorted(unknown)}"
        )

    raw_type = options.get("register_type", options.get("type"))
    register_type = _normalize_register_type(raw_type)

    raw_address = options.get("address")
    address = _strict_int(
        raw_address,
        f"Modbus point '{point.point_id}': address",
    )
    if address < 0:
        raise ConfigError(
            f"Modbus point '{point.point_id}': address must be >= 0"
        )

    expected_count = _count_for_data_type(register_type, point.raw_type.name)
    raw_count = options.get("count")
    count = (
        expected_count
        if raw_count is None
        else _strict_int(raw_count, f"Modbus point '{point.point_id}': count")
    )
    if count <= 0:
        raise ConfigError(f"Modbus point '{point.point_id}': count must be > 0")
    if count < expected_count:
        raise ConfigError(
            f"Modbus point '{point.point_id}': count {count} is too small for "
            f"{point.raw_type.name} (requires {expected_count})"
        )

    raw_word_order = options.get("word_order")
    word_order = (
        default_word_order
        if raw_word_order is None
        else _strict_string(
            raw_word_order,
            f"Modbus point '{point.point_id}': word_order",
        ).lower()
    )
    if word_order not in _VALID_WORD_ORDERS:
        raise ConfigError(
            f"Modbus point '{point.point_id}': invalid word_order '{word_order}'"
        )

    if (
        register_type in _READ_ONLY_TYPES
        and point.access in (PointAccess.WRITE, PointAccess.READ_WRITE)
    ):
        raise ConfigError(
            f"Modbus point '{point.point_id}': {register_type} is read-only but "
            f"access is '{point.access.value}'"
        )

    return ModbusPoint(
        point_id=point.point_id,
        register_type=register_type,
        address=address,
        count=count,
        data_type=point.raw_type.name,
        word_order=word_order,
    )


def group_consecutive_reads(
    points: list[ModbusPoint],
    *,
    max_gap: int = 8,
    max_registers_per_request: int = 125,
) -> list[list[ModbusPoint]]:
    """按 function code 和地址把读取点合并为合法 Modbus 请求。"""
    if not points:
        return []

    by_type: dict[str, list[ModbusPoint]] = {}
    for point in points:
        by_type.setdefault(point.register_type, []).append(point)

    groups: list[list[ModbusPoint]] = []
    for register_type in sorted(by_type):
        ordered = sorted(by_type[register_type], key=lambda item: item.address)
        current = [ordered[0]]
        previous_end = ordered[0].address + ordered[0].count

        for point in ordered[1:]:
            span = (
                max(previous_end, point.address + point.count)
                - current[0].address
            )
            if (
                point.address - previous_end <= max_gap
                and span <= max_registers_per_request
            ):
                current.append(point)
            else:
                groups.append(current)
                current = [point]
            previous_end = max(
                previous_end,
                point.address + point.count,
            )
        groups.append(current)
    return groups


def _normalize_register_type(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            "Modbus point requires 'register_type' or 'type' string option"
        )
    normalized = _REGISTER_TYPE_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ConfigError(f"invalid Modbus register type '{value}'")
    return normalized


def _count_for_data_type(register_type: str, data_type: str) -> int:
    if register_type in {"coil", "discrete_input"}:
        return 1
    count = _REGISTER_COUNTS.get(data_type)
    if count is None:
        raise ConfigError(
            f"Modbus data type '{data_type}' is unsupported for register access"
        )
    return count


def _strict_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} must be an integer")
    return value


def _strict_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value.strip()
