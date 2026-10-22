"""IEC104 点地址映射：完整协议地址为 ``(common_address, ioa)``。

不同 CA 下允许相同 IOA；同一 CA 内 IOA 唯一。点的规范地址是
``IEC104Point(common_address, ioa, type_id)``，所有索引、请求关联与
接收路由都必须使用完整地址，禁止再以裸 IOA 跨 CA 寻址。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.application.errors import ConfigError
from core.domain import Point, PointAccess

_ALLOWED_OPTIONS = frozenset({"ca", "ioa", "type", "type_id", "data_type"})
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
_SUPPORTED_MONITORING_PREFIX = "M_"


@dataclass(frozen=True, slots=True)
class IEC104Point:
    """规范化 IEC104 点定义（完整协议地址）。"""

    point_id: str
    common_address: int
    ioa: int
    type_id: str | None


def iec104_point(
    point_id: str,
    *,
    ioa: int,
    common_address: int = 1,
    type_id: str | None = None,
) -> IEC104Point:
    """构造并校验一个 IEC104Point（测试/装配显式构造用）。"""
    ca = _validate_ca(common_address, point_id)
    ioa_value = _validate_ioa(ioa, point_id)
    normalized_type = _validate_type_id(type_id, point_id)
    return IEC104Point(
        point_id=point_id,
        common_address=ca,
        ioa=ioa_value,
        type_id=normalized_type,
    )


def parse_iec104_point(point: Point, *, default_ca: int) -> IEC104Point:
    """把领域 Point 解析为 IEC104 地址定义；未指定 ``ca`` 时归缺省 CA。"""
    options = point.ext
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(
            f"IEC104 point '{point.point_id}' has unknown options: {sorted(unknown)}"
        )

    raw_ca = options.get("ca")
    if raw_ca is None:
        ca = default_ca
    else:
        if isinstance(raw_ca, bool) or not isinstance(raw_ca, int):
            raise ConfigError(f"IEC104 point '{point.point_id}': ca must be an integer")
        ca = raw_ca
    ca = _validate_ca(ca, point.point_id)

    raw_ioa = options.get("ioa")
    if isinstance(raw_ioa, bool) or not isinstance(raw_ioa, int):
        raise ConfigError(f"IEC104 point '{point.point_id}': ioa must be an integer")
    ioa = _validate_ioa(raw_ioa, point.point_id)

    type_id = _validate_type_id(
        options.get("type_id", options.get("type")),
        point.point_id,
    )
    return IEC104Point(
        point_id=point.point_id,
        common_address=ca,
        ioa=ioa,
        type_id=type_id,
    )



def build_iec104_index(
    points: list[Point],
    *,
    common_addresses: tuple[int, ...],
    default_ca: int,
) -> tuple[dict[str, IEC104Point], dict[tuple[int, int], IEC104Point]]:
    """构建 point_id → 点、``(ca, ioa)`` → 点双向索引。

    校验：点 CA 必须在连接配置的 CA 集合内；同 CA 下 IOA 不得重复；
    遥测点（M_*）与控制点（C_*）的 type_id 必须与访问方向一致。
    """
    if default_ca not in common_addresses:
        raise ConfigError(f"IEC104 default CA {default_ca} is not in {list(common_addresses)}")
    by_id: dict[str, IEC104Point] = {}
    by_address: dict[tuple[int, int], IEC104Point] = {}
    for point in points:
        mapped = parse_iec104_point(point, default_ca=default_ca)
        if mapped.common_address not in common_addresses:
            raise ConfigError(
                f"IEC104 point '{mapped.point_id}' uses ca={mapped.common_address} "
                f"which is not configured in common_addresses={list(common_addresses)}"
            )
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
    is_monitoring = type_id.startswith(_SUPPORTED_MONITORING_PREFIX)
    if writable and not is_command:
        raise ConfigError(
            f"IEC104 writable point '{mapped.point_id}' must use a C_* command type, "
            f"got '{type_id}'"
        )
    if point.access is PointAccess.READ and not is_monitoring:
        raise ConfigError(
            f"IEC104 read-only point '{mapped.point_id}' must use an M_* type, "
            f"got '{type_id}'"
        )


def _validate_ca(ca: object, point_id: str) -> int:
    if isinstance(ca, bool) or not isinstance(ca, int) or not 0 <= ca <= _MAX_CA:
        raise ConfigError(f"IEC104 point '{point_id}': ca must be an integer in 0..65535")
    return ca


def _validate_ioa(ioa: object, point_id: str) -> int:
    if isinstance(ioa, bool) or not isinstance(ioa, int) or not 0 <= ioa <= _MAX_IOA:
        raise ConfigError(
            f"IEC104 point '{point_id}': ioa must be an integer in 0..{_MAX_IOA}"
        )
    return ioa


def _validate_type_id(type_id: object, point_id: str) -> str | None:
    if type_id is None:
        return None
    if not isinstance(type_id, str) or not type_id.strip():
        raise ConfigError(
            f"IEC104 point '{point_id}': type/type_id must be a non-empty string"
        )
    return type_id.strip().upper()
