"""Modbus 点地址解析与批量读取分组。

PointConfig.address 的动态字段定义 register_type、0-based address、可选 count
和 word_order。本模块只做纯配置转换与分组，不执行网络 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub_core.config.schema import PointAddress, PointConfig
from wind_hub_core.model.errors import ConfigError

# register_type 的规范名称及兼容别名。
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

# holding/input 点不同数据类型占用的 16-bit register 数。
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


@dataclass(frozen=True)
class ModbusPoint:
    """单个 Modbus 点的规范化地址与数据类型描述。"""

    point_id: str
    register_type: str
    """规范化 register_type：coil、discrete_input、holding 或 input。"""

    address: int
    """0-based coil/register 地址。"""

    count: int
    """读取该点所需的 coil 数或 16-bit register 数。"""

    data_type: str
    """点表声明的数据类型。"""

    word_order: str = "little_endian"
    """多寄存器 word order。

    big_endian：低地址寄存器保存高 16 位；
    little_endian：低地址寄存器保存低 16 位。
    """


def _extra(address: PointAddress, key: str) -> object:
    """读取 PointAddress 动态协议字段；不存在时返回 None。"""
    return (address.model_extra or {}).get(key)


def _as_int(value: object, what: str) -> int:
    """把动态协议字段收敛为 int。

    bool 虽是 int 子类，但地址/count 不允许布尔语义，因此显式拒绝。

    Raises:
        ConfigError: 输入不是严格整数。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{what}: must be an integer, got {type(value).__name__}")
    return value


def _normalize_register_type(raw: object) -> str:
    if raw is None:
        raise ConfigError(
            "Modbus point: missing 'register_type' in address "
            "(expected coil/discrete_input/holding/input)"
        )
    key = str(raw).lower()
    if key not in _REGISTER_TYPE_ALIASES:
        raise ConfigError(
            f"Modbus point: invalid register_type '{raw}'; "
            f"must be one of coil/discrete_input/holding/input"
        )
    return _REGISTER_TYPE_ALIASES[key]


def _count_for_data_type(register_type: str, data_type: str) -> int:
    if register_type in ("coil", "discrete_input"):
        # bit-addressable 点固定占一个 coil，与 data_type 无关。
        return 1
    count = _REGISTER_COUNTS.get(data_type)
    if count is None:
        raise ConfigError(
            f"Modbus point: data_type '{data_type}' is not supported for "
            f"register reads (supported: {sorted(_REGISTER_COUNTS)})"
        )
    return count


def parse_point(point: PointConfig, default_word_order: str = "little_endian") -> ModbusPoint:
    """把 PointConfig 转换为 ModbusPoint。

    Args:
        point: 点表定义。
        default_word_order: 设备级默认 word order。

    Returns:
        规范化后的 ModbusPoint。

    Raises:
        ConfigError: register_type/address/count/word_order 非法，或数据类型不支持。
    """
    address = point.address
    register_type = _normalize_register_type(
        _extra(address, "register_type")
        if _extra(address, "register_type") is not None
        else address.type
    )

    raw_addr = _extra(address, "address")
    if raw_addr is None:
        raise ConfigError(f"Modbus point '{point.point_id}': missing 'address'")
    addr = _as_int(raw_addr, f"Modbus point '{point.point_id}': address")
    if addr < 0:
        raise ConfigError(f"Modbus point '{point.point_id}': address must be >= 0, got {addr}")

    explicit_count = _extra(address, "count")
    if explicit_count is not None:
        count = _as_int(explicit_count, f"Modbus point '{point.point_id}': count")
        if count <= 0:
            raise ConfigError(f"Modbus point '{point.point_id}': count must be > 0, got {count}")
    else:
        count = _count_for_data_type(register_type, point.data_type)

    word_order = str(_extra(address, "word_order") or default_word_order)
    if word_order not in _VALID_WORD_ORDERS:
        raise ConfigError(
            f"Modbus point '{point.point_id}': invalid word_order '{word_order}'; "
            f"must be one of {sorted(_VALID_WORD_ORDERS)}"
        )

    return ModbusPoint(
        point_id=point.point_id,
        register_type=register_type,
        address=addr,
        count=count,
        data_type=point.data_type,
        word_order=word_order,
    )


def group_consecutive_reads(
    points: list[ModbusPoint],
    max_gap: int = 8,
    max_registers_per_request: int = 125,
) -> list[list[ModbusPoint]]:
    """把点位分组成可由单个 Modbus 请求覆盖的连续区间。

    Args:
        points: 已解析 ModbusPoint 列表。
        max_gap: 相邻点允许合并的最大地址间隙。
        max_registers_per_request: 单请求允许覆盖的最大地址跨度，默认 125。

    Returns:
        按 register_type 隔离、按地址排序后的读取分组。

    Notes:
        不同 register_type 对应不同 function code，不能合并。分组跨度包含组内空洞，
        并严格限制在协议请求上限内。
    """
    if not points:
        return []

    by_type: dict[str, list[ModbusPoint]] = {}
    for p in points:
        by_type.setdefault(p.register_type, []).append(p)

    groups: list[list[ModbusPoint]] = []
    for register_type in sorted(by_type):
        ordered = sorted(by_type[register_type], key=lambda p: p.address)
        current: list[ModbusPoint] = [ordered[0]]
        prev_end = ordered[0].address + ordered[0].count
        for p in ordered[1:]:
            span = max(prev_end, p.address + p.count) - current[0].address
            if p.address - prev_end <= max_gap and span <= max_registers_per_request:
                current.append(p)
            else:
                groups.append(current)
                current = [p]
            prev_end = max(prev_end, p.address + p.count)
        groups.append(current)

    return groups
