"""IEC104 领域语义与 c104 类型之间的纯映射。

本模块只做 wind-hub 领域模型（PointValue / Quality / Command / Sink 导出值）
与 c104 公开类型（Point / Information / Quality / Type / Double 等）之间的
转换，不做任何 APCI/ASDU 二进制编码——wire protocol 完全由
c104/lib60870-C 承担。

c104 是可选依赖（extras ``iec104``）：模块导入不强制要求 c104 可用，
运行时调用方（driver / server / sink）在进入本模块前已确认依赖存在。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from wind_hub_core.model.command import Command
from wind_hub_core.model.point import PointValue, Quality

if TYPE_CHECKING:
    import c104
else:  # pragma: no cover - 依赖存在性由调用方守卫
    try:
        import c104
    except ImportError:
        c104 = None  # type: ignore[assignment]

# wind-hub PointValue.source 的协议标识。
SOURCE = "iec104"


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------


def quality_from_c104(quality: c104.Quality | c104.BinaryCounterQuality | None) -> Quality:
    """把 c104 品质标志映射为统一 Quality。

    ``Invalid`` 视为 BAD；``NonTopical`` / ``Substituted`` / ``Blocked`` /
    ``Overflow`` 视为 UNCERTAIN；无标志为 GOOD。c104 的 flags 类型不支持
    按位运算，经 ``value`` 位掩码判定。
    """
    if quality is None:
        return Quality.GOOD
    bits = quality.value
    if bits & c104.Quality.Invalid.value:
        return Quality.BAD
    if bits & (
        c104.Quality.NonTopical.value
        | c104.Quality.Substituted.value
        | c104.Quality.Blocked.value
        | c104.Quality.Overflow.value
    ):
        return Quality.UNCERTAIN
    return Quality.GOOD


def quality_to_c104(quality: Quality) -> c104.Quality:
    """把统一 Quality 映射为 c104 品质标志（从站侧上送用）。"""
    if quality is Quality.BAD:
        return c104.Quality.Invalid
    if quality is Quality.UNCERTAIN:
        return c104.Quality.NonTopical
    return c104.Quality()


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


def timestamp_from_c104(recorded_at: datetime | None) -> datetime | None:
    """归一化 c104 时标为 UTC aware datetime。

    c104 对 CP56Time2a 返回 naive datetime；CP56Time2a 本身不带时区，
    按 IEC 规范语义解释为 UTC。
    """
    if recorded_at is None:
        return None
    if recorded_at.tzinfo is None:
        return recorded_at.replace(tzinfo=UTC)
    return recorded_at.astimezone(UTC)


# ---------------------------------------------------------------------------
# 客户端：c104 Point → PointValue
# ---------------------------------------------------------------------------


def value_from_c104(value: Any) -> Any:
    """把 c104 点值转换为 wind-hub 标量。

    ``Double`` / ``Step`` 等 IntEnum 转 int（Double.ON=2、OFF=1，与旧实现的
    双点 int 语义一致）；``NormalizedFloat`` 转 float；bool/int/float 原样
    透传；无法映射的结构化值返回 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, IntEnum):
        return int(value)
    if isinstance(value, int | float):
        return value
    # c104.NormalizedFloat 等业务包装类型均可 float()。
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def point_value_from_c104(point: c104.Point, point_id: str) -> PointValue:
    """把 c104 客户端点当前状态转换为 PointValue（device_id 由上层补盖）。"""
    timestamp = timestamp_from_c104(point.recorded_at)
    kwargs: dict[str, Any] = {}
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return PointValue(
        device_id="",
        point_id=point_id,
        value=value_from_c104(point.value),
        quality=quality_from_c104(point.quality),
        source=SOURCE,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 客户端：Command → c104 控制点类型与值
# ---------------------------------------------------------------------------


def command_to_c104(cmd: Command, data_type: str) -> tuple[c104.Type, Any]:
    """根据 Command.value 与点 data_type 选择 c104 控制点类型并转换值。

    判定树与旧实现保持一致：bool（或 bool data_type）→ C_SC_NA_1；
    float（或 float data_type）→ C_SE_NC_1；int 按数值与 data_type 分派
    单点/双点/设点。Python bool 是 int 子类，bool 判定必须先于 int。

    Raises:
        ValueError: 值类型不受支持。
    """
    val = cmd.value

    if isinstance(val, bool) or data_type == "bool":
        return c104.Type.C_SC_NA_1, bool(val)

    if isinstance(val, float) or data_type in ("float32", "float64"):
        return c104.Type.C_SE_NC_1, float(val)

    if isinstance(val, int):
        # value=1 在单点/双点语义间有歧义，用 data_type 辅助判定。
        if val == 2 or (val == 1 and data_type not in ("", "bool")):
            return c104.Type.C_DC_NA_1, _double_from_int(val)
        if val in (0, 1):
            return c104.Type.C_SC_NA_1, bool(val)
        return c104.Type.C_SE_NC_1, float(val)

    raise ValueError(f"unsupported value type {type(val).__name__} for control command")


def _double_from_int(value: int) -> c104.Double:
    """把双点 int（1=OFF，2=ON）映射为 c104.Double；其余值拒绝。"""
    if value == 1:
        return c104.Double.OFF
    if value == 2:
        return c104.Double.ON
    raise ValueError(f"invalid double command value {value} (expected 1=OFF or 2=ON)")


def _double_state(value: int) -> c104.Double:
    """把 DPI int（0-3，含中间/不确定态）映射为 c104.Double，供从站监视点使用。"""
    # 函数内构造避免模块导入期依赖 c104（可选依赖守卫）。
    states = {
        0: c104.Double.INTERMEDIATE,
        1: c104.Double.OFF,
        2: c104.Double.ON,
        3: c104.Double.INDETERMINATE,
    }
    if value not in states:
        raise ValueError(f"invalid double point state {value} (expected 0..3)")
    return states[value]


# ---------------------------------------------------------------------------
# 从站侧：Sink type_id 字符串 → c104 类型与 Information
# ---------------------------------------------------------------------------

# 合法 type_id 由 IEC104SinkAddress 的 Literal 白名单约束，本表是运行期防线。
_SINK_TYPE_IDS = frozenset(
    {
        "M_SP_NA_1",
        "M_DP_NA_1",
        "M_ME_NA_1",
        "M_ME_NB_1",
        "M_ME_NC_1",
        "M_SP_TB_1",
        "M_DP_TB_1",
        "M_ME_TF_1",
    }
)


def sink_point_type(type_id: str) -> c104.Type:
    """把 IEC104SinkAddress.type_id 字符串映射为 c104.Type。

    Raises:
        ValueError: type_id 不在白名单（说明配置验证边界被绕过）。
    """
    if type_id not in _SINK_TYPE_IDS:
        raise ValueError(f"Unsupported IEC104 sink type_id '{type_id}'")
    # _SINK_TYPE_IDS 白名单保证成员存在；pybind 枚举不支持下表访问。
    member: c104.Type = getattr(c104.Type, type_id)
    return member


def build_monitor_info(
    type_id: str,
    value: Any,
    quality: Quality,
    timestamp: datetime,
) -> c104.Information:
    """按 type_id 构造携带值、品质与源时标的 c104 Information。

    recorded_at 透传源采集时标，保证带时标类型（M_SP_TB_1 / M_DP_TB_1 /
    M_ME_TF_1）的总召与上送保留采集时刻而非从站处理时刻。
    """
    q = quality_to_c104(quality)
    ts = timestamp.astimezone(UTC)
    if type_id in ("M_SP_NA_1", "M_SP_TB_1"):
        return c104.SingleInfo(on=bool(value), quality=q, recorded_at=ts)
    if type_id in ("M_DP_NA_1", "M_DP_TB_1"):
        return c104.DoubleInfo(state=_double_state(int(value)), quality=q, recorded_at=ts)
    if type_id == "M_ME_NA_1":
        return c104.NormalizedInfo(
            actual=c104.NormalizedFloat(float(value)), quality=q, recorded_at=ts
        )
    if type_id == "M_ME_NB_1":
        return c104.ScaledInfo(actual=c104.Int16(int(value)), quality=q, recorded_at=ts)
    # M_ME_NC_1 / M_ME_TF_1
    return c104.ShortInfo(actual=float(value), quality=q, recorded_at=ts)
