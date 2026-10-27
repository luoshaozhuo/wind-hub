"""IEC104 点地址映射：完整协议地址为 ``(common_address, ioa)``。

不同 CA 下允许相同 IOA；同一 CA 内 IOA 唯一。点的规范地址是
``IEC104Point(common_address, ioa, type_id)``，所有索引、请求关联与
接收路由都必须使用完整地址，禁止再以裸 IOA 跨 CA 寻址。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from core.application.errors import ConfigError
from core.domain import Point, PointAccess

_ALLOWED_OPTIONS = frozenset(
    {"ca", "ioa", "type", "type_id", "data_type", "command_mode", "confirmation"}
)
_MAX_IOA = 0xFFFFFF
_MAX_CA = 65535
_COMMAND_TYPE_IDS = frozenset(
    {
        "C_SC_NA_1",
        "C_DC_NA_1",
        "C_SE_NA_1",
        "C_SE_NB_1",
        "C_SE_NC_1",
    }
)
_MONITORING_TYPE_IDS = frozenset(
    {
        "M_SP_NA_1",
        "M_SP_TB_1",
        "M_DP_NA_1",
        "M_DP_TB_1",
        "M_ST_NA_1",
        "M_ST_TB_1",
        "M_BO_NA_1",
        "M_BO_TB_1",
        "M_ME_NA_1",
        "M_ME_ND_1",
        "M_ME_TD_1",
        "M_ME_NB_1",
        "M_ME_TE_1",
        "M_ME_NC_1",
        "M_ME_TF_1",
        "M_IT_NA_1",
        "M_IT_TB_1",
        "M_PS_NA_1",
    }
)


@dataclass(frozen=True, slots=True)
class IEC104Point:
    """规范化 IEC104 点定义（完整协议地址）。"""

    point_id: str
    common_address: int
    ioa: int
    type_id: str | None
    command_mode: Literal["direct", "select_execute"] = "direct"
    confirmation: Literal["act_con", "act_term"] = "act_term"


def parse_iec104_point(point: Point) -> IEC104Point:
    """把领域 Point 解析为 IEC104 地址定义；CA 必须显式指定。"""
    options = point.ext
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(f"IEC104 point '{point.point_id}' has unknown options: {sorted(unknown)}")

    ca = _validate_ca(options.get("ca"), point.point_id)

    raw_ioa = options.get("ioa")
    if isinstance(raw_ioa, bool) or not isinstance(raw_ioa, int):
        raise ConfigError(f"IEC104 point '{point.point_id}': ioa must be an integer")
    ioa = _validate_ioa(raw_ioa, point.point_id)

    type_id = _validate_type_id(
        options.get("type_id", options.get("type")),
        point.point_id,
    )
    if type_id in _MONITORING_TYPE_IDS and (
        "command_mode" in options or "confirmation" in options
    ):
        raise ConfigError(f"IEC104 monitoring point '{point.point_id}' has command options")
    command_mode = options.get("command_mode", "direct")
    confirmation = options.get("confirmation", "act_term")
    if command_mode not in ("direct", "select_execute"):
        raise ConfigError(f"IEC104 point '{point.point_id}': invalid command_mode")
    if confirmation not in ("act_con", "act_term"):
        raise ConfigError(f"IEC104 point '{point.point_id}': invalid confirmation")
    return IEC104Point(
        point_id=point.point_id,
        common_address=ca,
        ioa=ioa,
        type_id=type_id,
        command_mode=cast(Literal["direct", "select_execute"], command_mode),
        confirmation=cast(Literal["act_con", "act_term"], confirmation),
    )


def build_iec104_index(
    points: list[Point],
) -> tuple[dict[str, IEC104Point], dict[tuple[int, int], IEC104Point]]:
    """构建 point_id 与 (CA, IOA) 双向索引，校验地址唯一及读写类型。"""
    by_id: dict[str, IEC104Point] = {}
    by_address: dict[tuple[int, int], IEC104Point] = {}
    for point in points:
        mapped = parse_iec104_point(point)
        existing = by_address.get((mapped.common_address, mapped.ioa))
        if existing is not None:
            raise ConfigError(
                f"IEC104 points '{existing.point_id}' and '{mapped.point_id}' share "
                f"duplicate address (ca={mapped.common_address}, ioa={mapped.ioa})"
            )
        _validate_access_type(point, mapped)
        by_id[mapped.point_id] = mapped
        by_address[(mapped.common_address, mapped.ioa)] = mapped
    return by_id, by_address


def validate_iec104_write_type(point: IEC104Point) -> None:
    """校验写点使用 Driver 明确支持的 IEC104 控制类型。"""
    if point.type_id is None:
        raise ConfigError(f"IEC104 writable point '{point.point_id}' requires type_id")
    if point.type_id not in _COMMAND_TYPE_IDS:
        raise ConfigError(
            f"IEC104 writable point '{point.point_id}' uses unsupported "
            f"command type '{point.type_id}'"
        )


def _validate_access_type(point: Point, mapped: IEC104Point) -> None:
    """访问方向与 M_*/C_* 类型必须一致，禁止遥测/控制点混用。"""
    type_id = mapped.type_id
    writable = point.access in (PointAccess.WRITE, PointAccess.READ_WRITE)
    if type_id is None:
        # 读取方向无需本地类型：类型随对端上报报文确定。
        if writable:
            validate_iec104_write_type(mapped)
        return
    is_command = type_id in _COMMAND_TYPE_IDS
    is_monitoring = type_id in _MONITORING_TYPE_IDS
    if writable and not is_command:
        raise ConfigError(
            f"IEC104 writable point '{mapped.point_id}' must use a C_* command type, "
            f"got '{type_id}'"
        )
    if point.access is PointAccess.READ and not is_monitoring:
        raise ConfigError(
            f"IEC104 read-only point '{mapped.point_id}' must use an M_* type, " f"got '{type_id}'"
        )


def _validate_ca(ca: object, point_id: str) -> int:
    if isinstance(ca, bool) or not isinstance(ca, int) or not 0 <= ca <= _MAX_CA:
        raise ConfigError(f"IEC104 point '{point_id}': ca must be an integer in 0..65535")
    return ca


def _validate_ioa(ioa: object, point_id: str) -> int:
    if isinstance(ioa, bool) or not isinstance(ioa, int) or not 0 <= ioa <= _MAX_IOA:
        raise ConfigError(f"IEC104 point '{point_id}': ioa must be an integer in 0..{_MAX_IOA}")
    return ioa


def _validate_type_id(type_id: object, point_id: str) -> str | None:
    if type_id is None:
        return None
    if not isinstance(type_id, str) or not type_id.strip():
        raise ConfigError(f"IEC104 point '{point_id}': type/type_id must be a non-empty string")
    return type_id.strip().upper()
