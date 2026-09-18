"""File sink 滚动策略 —— 把滚动判断从写入循环中抽离为可组合的策略对象。

策略接口 :class:`RotationPolicy` 只有两个方法：``should_rotate`` 判断当前文件
是否该滚动，``rotation_suffix`` 生成归档文件名的时间戳后缀。``FileSink`` 在
``params`` 里读到 ``max_size_mb`` / ``max_age_hours`` 后，经
:func:`build_rotation` 构造成具体的策略实例，写入循环里只问策略、不再内联
比较大小与时长。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from wind_hub.domain.model.errors import ConfigError

_BYTES_PER_MB = 1024 * 1024
_SECONDS_PER_HOUR = 3600.0


class RotationPolicy(Protocol):
    """滚动策略接口。"""

    def should_rotate(self, current_size: int, age_seconds: float) -> bool:
        """判断当前文件是否应该滚动。

        Args:
            current_size: 当前文件字节数。
            age_seconds: 当前文件自打开起经过的秒数。
        """
        ...

    def rotation_suffix(self, timestamp: datetime) -> str:
        """返回滚动文件的后缀（如 ``20260916_143025_123456``，微秒级防碰撞）。"""
        ...


class SizeRotation:
    """按大小滚动——文件达到 ``max_size_mb`` 即滚动。"""

    def __init__(self, max_size_mb: float) -> None:
        self._max_bytes = int(max_size_mb * _BYTES_PER_MB)

    def should_rotate(self, current_size: int, age_seconds: float) -> bool:
        return current_size >= self._max_bytes

    def rotation_suffix(self, timestamp: datetime) -> str:
        return timestamp.strftime("%Y%m%d_%H%M%S_%f")


class TimeRotation:
    """按时间滚动——文件打开超过 ``max_age_hours`` 即滚动。"""

    def __init__(self, max_age_hours: float) -> None:
        self._max_seconds = max_age_hours * _SECONDS_PER_HOUR

    def should_rotate(self, current_size: int, age_seconds: float) -> bool:
        return age_seconds >= self._max_seconds

    def rotation_suffix(self, timestamp: datetime) -> str:
        return timestamp.strftime("%Y%m%d_%H%M%S_%f")


class CompositeRotation:
    """组合多个策略——任一策略触发即滚动。"""

    def __init__(self, policies: list[RotationPolicy]) -> None:
        if not policies:
            raise ConfigError("CompositeRotation requires at least one policy")
        self._policies = policies

    def should_rotate(self, current_size: int, age_seconds: float) -> bool:
        return any(policy.should_rotate(current_size, age_seconds) for policy in self._policies)

    def rotation_suffix(self, timestamp: datetime) -> str:
        return self._policies[0].rotation_suffix(timestamp)


class NoRotation:
    """不滚动。"""

    def should_rotate(self, current_size: int, age_seconds: float) -> bool:
        return False

    def rotation_suffix(self, timestamp: datetime) -> str:
        return timestamp.strftime("%Y%m%d_%H%M%S_%f")


def build_rotation(params: dict[str, Any]) -> RotationPolicy:
    """从 ``SinkConfig.params`` 构建滚动策略。

    ``max_size_mb`` / ``max_age_hours`` 任一为正值都参与滚动；都为 ``None``
    （或未设置）时返回 :class:`NoRotation`。数值校验沿用 ``FileSink`` 的
    规则：必须是正数，否则抛 :class:`ConfigError`。
    """
    max_size_mb = params.get("max_size_mb")
    max_age_hours = params.get("max_age_hours")

    if max_size_mb is not None:
        _validate_positive(max_size_mb, "max_size_mb")
    if max_age_hours is not None:
        _validate_positive(max_age_hours, "max_age_hours")

    policies: list[RotationPolicy] = []
    if max_size_mb is not None:
        policies.append(SizeRotation(float(max_size_mb)))
    if max_age_hours is not None:
        policies.append(TimeRotation(float(max_age_hours)))

    if not policies:
        return NoRotation()
    if len(policies) == 1:
        return policies[0]
    return CompositeRotation(policies)


def _validate_positive(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"FileSink '{field}' must be a positive number, got {value!r}")
