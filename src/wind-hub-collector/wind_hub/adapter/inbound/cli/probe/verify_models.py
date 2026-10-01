"""probe verify 的结果模型——纯数据、无 I/O，可独立单测。

三级结构（决策 6）：:class:`PointVerifyResult`（单点）→
:class:`DeviceVerifyResult`（单设备）→ :class:`VerifyResult`（整体），
汇总统计全部用 property 从明细推导，不存在可不一致的冗余字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 质量 BAD 的字符串值（与 domain Quality.BAD.value 一致；模型层不依赖
# domain 枚举，保持纯数据、便于 JSON 往返）。
_QUALITY_BAD = "bad"


@dataclass(frozen=True)
class PointVerifyResult:
    """单个点的验证结果。

    Attributes:
        point_id: 点标识。
        ok: 是否成功返回（读失败 vs 读成功但质量 BAD 是两种现场含义，
            决策 5）。
        value: 返回的值（成功时）。
        quality: 质量码字符串（成功时，如 "good" / "bad"）。
        error: 失败原因（失败时）。
    """

    point_id: str
    ok: bool
    value: Any | None = None
    quality: str | None = None
    error: str | None = None

    @property
    def quality_bad(self) -> bool:
        """读成功但质量 BAD（决策 5：单列一节，与读失败区分）。"""
        return self.ok and self.quality == _QUALITY_BAD

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/YAML 输出用的字典形式。"""
        return {
            "point_id": self.point_id,
            "ok": self.ok,
            "value": self.value,
            "quality": self.quality,
            "error": self.error,
        }


@dataclass(frozen=True)
class DeviceVerifyResult:
    """单个设备的验证结果（决策 10：connect 失败时所有点标记 fail）。"""

    device_id: str
    protocol: str
    connect_ok: bool
    connect_error: str | None
    points: list[PointVerifyResult]

    @property
    def total(self) -> int:
        """点表总数。"""
        return len(self.points)

    @property
    def ok_count(self) -> int:
        """成功返回的点数（含质量 BAD——能返回但值不可信）。"""
        return sum(1 for p in self.points if p.ok)

    @property
    def fail_count(self) -> int:
        """读失败的点数。"""
        return sum(1 for p in self.points if not p.ok)

    @property
    def bad_quality_count(self) -> int:
        """读成功但质量 BAD 的点数（决策 5）。"""
        return sum(1 for p in self.points if p.quality_bad)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/YAML 输出用的字典形式。"""
        return {
            "device_id": self.device_id,
            "protocol": self.protocol,
            "connect_ok": self.connect_ok,
            "connect_error": self.connect_error,
            "total": self.total,
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "bad_quality_count": self.bad_quality_count,
            "points": [p.to_dict() for p in self.points],
        }


@dataclass(frozen=True)
class VerifyResult:
    """整体验证结果（决策 6/7）。"""

    devices: list[DeviceVerifyResult]
    duration_ms: float

    @property
    def total_points(self) -> int:
        """全部设备的点表总数。"""
        return sum(d.total for d in self.devices)

    @property
    def total_ok(self) -> int:
        """全部设备成功返回的点数。"""
        return sum(d.ok_count for d in self.devices)

    @property
    def total_fail(self) -> int:
        """全部设备读失败的点数。"""
        return sum(d.fail_count for d in self.devices)

    @property
    def total_bad_quality(self) -> int:
        """全部设备质量 BAD 的点数。"""
        return sum(d.bad_quality_count for d in self.devices)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/YAML 输出用的字典形式（含 summary 小节，决策 8）。"""
        return {
            "devices": [d.to_dict() for d in self.devices],
            "summary": {
                "total_devices": len(self.devices),
                "total_points": self.total_points,
                "total_ok": self.total_ok,
                "total_fail": self.total_fail,
                "total_bad_quality": self.total_bad_quality,
                "duration_ms": round(self.duration_ms, 1),
            },
        }
