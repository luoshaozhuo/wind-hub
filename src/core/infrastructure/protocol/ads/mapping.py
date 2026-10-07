"""ADS PointDefinition 地址与类型映射。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.application.config import ProtocolOptions
from core.application.errors import ConfigError
from core.domain import PointDefinition

_ALLOWED_OPTIONS = frozenset(
    {
        "symbol",
        "index_group",
        "index_offset",
        "data_type",
        "type",
        "size",
    }
)

_RAW_TO_ADS: dict[str, str] = {
    "bool": "BOOL",
    "int8": "SINT",
    "uint8": "USINT",
    "int16": "INT",
    "uint16": "UINT",
    "int32": "DINT",
    "uint32": "UDINT",
    "int64": "LINT",
    "uint64": "ULINT",
    "float32": "REAL",
    "float64": "LREAL",
    "str": "STRING",
}
_ADS_TYPE_SIZES: dict[str, int] = {
    "BOOL": 1,
    "SINT": 1,
    "USINT": 1,
    "INT": 2,
    "UINT": 2,
    "DINT": 4,
    "UDINT": 4,
    "LINT": 8,
    "ULINT": 8,
    "REAL": 4,
    "LREAL": 8,
    "STRING": 0,
}


@dataclass(frozen=True, slots=True)
class ADSPoint:
    """规范化 ADS 点地址。

    symbol 存在时，index 地址必须在当前 ADS session 建立后重新解析；
    配置中的 index 值仅作诊断参考，不能直接用于生产读写。
    """

    point_id: str
    index_group: int
    index_offset: int
    data_type: str
    size: int
    symbol: str | None = None
    address_resolved: bool = True

    def resolved(
        self,
        *,
        index_group: int,
        index_offset: int,
        size: int | None = None,
    ) -> ADSPoint:
        """返回绑定当前 ADS session 地址的新值对象。"""
        return replace(
            self,
            index_group=index_group,
            index_offset=index_offset,
            size=self.size if size is None else size,
            address_resolved=True,
        )

    def unresolved(self) -> ADSPoint:
        """使 symbol 派生地址失效。"""
        if self.symbol is None:
            return self
        return replace(self, address_resolved=False)


def parse_ads_point(
    point: PointDefinition,
    options: ProtocolOptions,
) -> ADSPoint:
    """把 PointDefinition 解析为纯内存 ADSPoint。"""
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(
            f"ADS point '{point.point_id}' has unknown options: {sorted(unknown)}"
        )

    symbol = _optional_string(options.get("symbol"), "symbol", point.point_id)
    raw_group = options.get("index_group")
    raw_offset = options.get("index_offset")
    if (raw_group is None) != (raw_offset is None):
        raise ConfigError(
            f"ADS point '{point.point_id}': index_group and index_offset "
            "must be configured together"
        )
    if symbol is None and raw_group is None:
        raise ConfigError(
            f"ADS point '{point.point_id}' requires symbol or index address"
        )

    index_group = (
        0
        if raw_group is None
        else _non_negative_int(raw_group, "index_group", point.point_id)
    )
    index_offset = (
        0
        if raw_offset is None
        else _non_negative_int(raw_offset, "index_offset", point.point_id)
    )

    explicit_type = options.get("data_type", options.get("type"))
    if explicit_type is None:
        ads_type = _RAW_TO_ADS.get(point.raw_type.name)
        if ads_type is None:
            raise ConfigError(
                f"ADS point '{point.point_id}': unsupported raw type "
                f"'{point.raw_type.name}'"
            )
    else:
        ads_type = _normalize_ads_type(
            explicit_type,
            point_id=point.point_id,
        )

    size = _ADS_TYPE_SIZES[ads_type]
    explicit_size = options.get("size")
    if explicit_size is not None:
        size = _non_negative_int(explicit_size, "size", point.point_id)

    if ads_type != "STRING" and size != _ADS_TYPE_SIZES[ads_type]:
        raise ConfigError(
            f"ADS point '{point.point_id}': size {size} does not match "
            f"{ads_type} width {_ADS_TYPE_SIZES[ads_type]}"
        )
    if ads_type == "STRING" and size < 0:
        raise ConfigError(f"ADS point '{point.point_id}': size must be >= 0")

    return ADSPoint(
        point_id=point.point_id,
        index_group=index_group,
        index_offset=index_offset,
        data_type=ads_type,
        size=size,
        symbol=symbol,
        address_resolved=symbol is None,
    )


def _normalize_ads_type(value: object, *, point_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"ADS point '{point_id}': data_type must be a non-empty string"
        )
    normalized = value.strip().upper()
    normalized = _RAW_TO_ADS.get(normalized.lower(), normalized)
    if normalized not in _ADS_TYPE_SIZES:
        raise ConfigError(
            f"ADS point '{point_id}': unsupported ADS type '{value}'"
        )
    return normalized


def _optional_string(
    value: object,
    name: str,
    point_id: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"ADS point '{point_id}': {name} must be a non-empty string"
        )
    return value.strip()


def _non_negative_int(
    value: object,
    name: str,
    point_id: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"ADS point '{point_id}': {name} must be an integer"
        )
    if value < 0:
        raise ConfigError(
            f"ADS point '{point_id}': {name} must be >= 0"
        )
    return value
