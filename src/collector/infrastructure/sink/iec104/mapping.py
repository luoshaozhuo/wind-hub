"""IEC104 从站侧（Sink）领域语义与 c104 类型之间的纯映射。

只覆盖 Sink 上送方向：统一 Quality → c104 品质标志、Sink type_id 字符串 →
c104 监视类型与 Information。客户端方向映射由 core 的 IEC104Driver 承担。

c104 是可选依赖（extras ``iec104``）：模块导入不强制要求 c104 可用，
运行时调用方（sink）在进入本模块前已确认依赖存在。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.application import Quality

if TYPE_CHECKING:
    import c104
else:  # pragma: no cover - 依赖存在性由调用方守卫
    try:
        import c104
    except ImportError:
        c104 = None  # type: ignore[assignment]


def quality_to_c104(quality: Quality) -> c104.Quality:
    """把统一 Quality 映射为 c104 品质标志（从站侧上送用）。"""
    if quality is Quality.BAD:
        return c104.Quality.Invalid
    if quality is Quality.UNCERTAIN:
        return c104.Quality.NonTopical
    return c104.Quality()


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
