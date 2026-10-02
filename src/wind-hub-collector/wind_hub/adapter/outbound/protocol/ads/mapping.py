"""ADS 点地址与数据类型映射。

PointConfig.address 的动态字段承载 symbol、index_group、index_offset、ADS 类型
覆盖和可选 size。本模块只做纯配置转换，不访问 PLC。

只要配置了 symbol，该点在这里都保持“地址未解析”状态；即使同时提供了
index_group/index_offset，也只把它们作为配置参考值保留，真正运行地址必须由
ADSDriver 在当前 ADS session 建立后通过 symbol 重新解析。纯 index-only 点则
直接视为已解析。address.model_extra 使用 Any 是 Pydantic 动态协议字段边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wind_hub.config.schema import PointConfig
from wind_hub.domain.model.errors import ConfigError

# Wind Hub data_type 到 ADS 类型名的规范映射；输入统一转大写后查表。
_WINDHUB_TO_ADS: dict[str, str] = {
    "BOOL": "BOOL",
    "INT8": "SINT",
    "UINT8": "USINT",
    "INT16": "INT",
    "UINT16": "UINT",
    "INT32": "DINT",
    "UINT32": "UDINT",
    "FLOAT32": "REAL",
    "FLOAT64": "LREAL",
    "STR": "STRING",
    "STRING": "STRING",
}

# ADS 类型到固定字节数；0 表示 STRING 等变长类型。
_ADS_TYPE_SIZES: dict[str, int] = {
    "BOOL": 1,
    "SINT": 1,
    "USINT": 1,
    "INT": 2,
    "UINT": 2,
    "DINT": 4,
    "UDINT": 4,
    "REAL": 4,
    "LREAL": 8,
    "STRING": 0,
}


@dataclass(frozen=True)
class ADSPoint:
    """单个 ADS 点的运行时地址描述。

    index_group/index_offset 仅在 address_resolved=True 时可用于读写。
    """

    point_id: str
    index_group: int
    index_offset: int
    data_type: str
    """ADS 基础类型名，例如 BOOL、INT、REAL、STRING。"""

    size: int
    """固定字节数；0 表示变长类型。"""

    symbol: str | None = None
    """PLC symbol name，仅用于地址解析与诊断；运行时读写使用 index 地址。"""

    address_resolved: bool = True
    """index_group/index_offset 是否来自明确配置或已完成的 symbol 解析。"""


def map_data_type(data_type: str) -> tuple[int, str]:
    """把 Wind Hub data_type 映射为 ADS 字节数和类型名。

    Args:
        data_type: 点表数据类型。

    Returns:
        (size_bytes, ads_type_name)。

    Raises:
        ConfigError: 不支持该数据类型。
    """
    ads_name = _WINDHUB_TO_ADS.get(data_type.upper())
    if ads_name is None:
        raise ConfigError(f"ADS: unsupported data_type '{data_type}'")
    return (_ADS_TYPE_SIZES[ads_name], ads_name)


def _normalize_ads_type(raw: object) -> str:
    """规范化 ADS 类型名，并接受 Wind Hub 类型名作为别名。

    Raises:
        ConfigError: 类型名不受支持。
    """
    name = str(raw).strip().upper()
    if name in _WINDHUB_TO_ADS:
        name = _WINDHUB_TO_ADS[name]
    if name not in _ADS_TYPE_SIZES:
        raise ConfigError(
            f"ADS: unsupported data type '{raw}'; " f"supported: {sorted(_ADS_TYPE_SIZES)}"
        )
    return name


def parse_point(point: PointConfig) -> ADSPoint:
    """把 PointConfig 转换为 ADSPoint。

    Args:
        point: 点表定义。

    Returns:
        已规范化类型和地址信息的 ADSPoint。symbol-only 点返回
        address_resolved=False，等待 driver 建连后解析。

    Raises:
        ConfigError: symbol/index 地址不完整、数据类型不支持或 size 非法。
    """
    address = point.address
    extra = address.model_extra or {}

    symbol = extra.get("symbol")
    symbol = str(symbol) if symbol is not None else None

    index_group = extra.get("index_group")
    index_offset = extra.get("index_offset")
    # 运行时安全网（配置加载阶段已做同样校验）：index 两字段必须成对；
    # symbol 与 index 可同时保留；运行时读写始终使用已解析的 index 地址。
    if (index_group is None) != (index_offset is None):
        raise ConfigError(
            f"ADS point '{point.point_id}': 'index_group' and 'index_offset' "
            f"must be configured together"
        )
    if symbol is None and index_group is None:
        raise ConfigError(
            f"ADS point '{point.point_id}': missing 'symbol' or "
            f"'index_group'/'index_offset' in address"
        )
    # 只要存在 symbol，运行地址就必须来自当前 PLC session 的 symbol 解析。
    # 同时配置的 index 仅保留作参考/诊断，不允许直接进入生产读写。
    address_resolved = symbol is None and index_group is not None and index_offset is not None
    # symbol-only 点使用 0/0 作为临时结构字段；address_resolved=False 时绝不参与读写。
    # Any 来自 Pydantic extra 的动态 YAML 边界，int() 会在返回 ADSPoint 前收敛类型。
    ig: Any = index_group if index_group is not None else 0
    io: Any = index_offset if index_offset is not None else 0

    explicit_type = extra.get("data_type") if extra.get("data_type") is not None else address.type
    if explicit_type is not None:
        ads_name = _normalize_ads_type(explicit_type)
        size = _ADS_TYPE_SIZES[ads_name]
    else:
        size, ads_name = map_data_type(point.data_type)

    explicit_size = extra.get("size")
    if explicit_size is not None:
        size = int(explicit_size)
        if size < 0:
            raise ConfigError(f"ADS point '{point.point_id}': size must be >= 0, got {size}")

    return ADSPoint(
        point_id=point.point_id,
        index_group=int(ig),
        index_offset=int(io),
        data_type=ads_name,
        size=size,
        symbol=symbol,
        address_resolved=address_resolved,
    )
