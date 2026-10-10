"""IEC104 Point 的 IOA 与控制语义映射。"""

from __future__ import annotations

from dataclasses import dataclass

from core.application.errors import ConfigError
from core.domain import Point

# ``data_type`` 由配置装配层无条件注入 Point.ext（与 Modbus 同口径），
# IEC104 映射不消费它，但必须放行，否则任何经配置加载的 IEC104 点表都无法装配。
_ALLOWED_OPTIONS = frozenset({"ioa", "type", "type_id", "data_type"})
_MAX_IOA = 0xFFFFFF
_COMMAND_TYPE_IDS = frozenset(
    {
        "C_SC_NA_1",
        "C_DC_NA_1",
        "C_SE_NA_1",
        "C_SE_NB_1",
        "C_SE_NC_1",
    }
)


@dataclass(frozen=True, slots=True)
class IEC104Point:
    """规范化 IEC104 点定义。"""

    point_id: str
    ioa: int
    type_id: str | None


def parse_iec104_point(
    point: Point,
) -> IEC104Point:
    """把 Point 解析为 IEC104 地址定义。"""
    options = point.ext
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(f"IEC104 point '{point.point_id}' has unknown options: {sorted(unknown)}")

    raw_ioa = options.get("ioa")
    if isinstance(raw_ioa, bool) or not isinstance(raw_ioa, int):
        raise ConfigError(f"IEC104 point '{point.point_id}': ioa must be an integer")
    if not 0 <= raw_ioa <= _MAX_IOA:
        raise ConfigError(f"IEC104 point '{point.point_id}': ioa must be in 0..{_MAX_IOA}")

    raw_type_id = options.get("type_id", options.get("type"))
    type_id: str | None
    if raw_type_id is None:
        type_id = None
    elif isinstance(raw_type_id, str) and raw_type_id.strip():
        type_id = raw_type_id.strip().upper()
    else:
        raise ConfigError(
            f"IEC104 point '{point.point_id}': type/type_id must be a non-empty string"
        )

    return IEC104Point(
        point_id=point.point_id,
        ioa=raw_ioa,
        type_id=type_id,
    )


def build_iec104_index(
    points: list[Point],
) -> tuple[dict[str, IEC104Point], dict[int, IEC104Point]]:
    """构建 point_id/IOA 双向索引，并拒绝重复 IOA。"""
    by_id: dict[str, IEC104Point] = {}
    by_ioa: dict[int, IEC104Point] = {}
    for point in points:
        mapped = parse_iec104_point(point)
        if mapped.ioa in by_ioa:
            other = by_ioa[mapped.ioa]
            raise ConfigError(
                f"IEC104 points '{other.point_id}' and '{mapped.point_id}' "
                f"share duplicate IOA {mapped.ioa}"
            )
        by_id[mapped.point_id] = mapped
        by_ioa[mapped.ioa] = mapped
    return by_id, by_ioa


def validate_iec104_write_type(point: IEC104Point) -> None:
    """校验写点使用当前 Driver 明确支持的 IEC104 控制类型。"""
    if point.type_id is None:
        raise ConfigError(f"IEC104 writable point '{point.point_id}' requires type_id")
    if point.type_id not in _COMMAND_TYPE_IDS:
        raise ConfigError(
            f"IEC104 writable point '{point.point_id}' uses unsupported "
            f"command type '{point.type_id}'"
        )
