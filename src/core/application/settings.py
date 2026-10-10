"""Application 运行参数，Site 标识只属于 Domain.Site。"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

_BACKPRESSURE_POLICIES = frozenset({"drop_old", "drop_new", "block"})


def _require_non_empty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_finite(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{label} must be finite")


@dataclass(frozen=True, slots=True)
class ADSLocalConfig:
    """system.yaml ``ads`` 段的完整契约：本机身份与凭据（restart-required）。"""

    local_ams_net_id: str
    local_ip: str
    username: str
    password: str

    def __post_init__(self) -> None:
        _require_non_empty(self.local_ams_net_id, "ads.local_ams_net_id")
        _require_non_empty(self.local_ip, "ads.local_ip")


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """跨进程共享的运行参数；``None`` 表示未配置，默认值由各进程自定。"""

    queue_maxsize: int | None = None
    backpressure_policy: str | None = None
    shutdown_timeout: float | None = None
    connect_timeout: float | None = None
    read_timeout: float | None = None
    write_timeout: float | None = None
    read_retries: int = 1
    retry_interval: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.read_retries, bool) or not isinstance(self.read_retries, int):
            raise ValueError("runtime.read_retries must be an integer")
        if self.read_retries < -1:
            raise ValueError("runtime.read_retries must be >= -1")
        if isinstance(self.retry_interval, bool) or not isinstance(
            self.retry_interval, int | float
        ):
            raise ValueError("runtime.retry_interval must be a number")
        _require_finite(self.retry_interval, "runtime.retry_interval")
        if self.retry_interval < 0:
            raise ValueError("runtime.retry_interval must be >= 0")
        object.__setattr__(self, "retry_interval", float(self.retry_interval))
        if self.queue_maxsize is not None:
            if isinstance(self.queue_maxsize, bool) or not isinstance(self.queue_maxsize, int):
                raise ValueError("runtime.queue_maxsize must be an integer")
            if self.queue_maxsize <= 0:
                raise ValueError("runtime.queue_maxsize must be > 0")
        if self.backpressure_policy is not None and (
            self.backpressure_policy not in _BACKPRESSURE_POLICIES
        ):
            raise ValueError(
                f"Invalid backpressure_policy '{self.backpressure_policy}'; "
                f"must be one of {sorted(_BACKPRESSURE_POLICIES)}"
            )
        for name in ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout"):
            value = getattr(self, name)
            if value is None:
                continue
            _require_finite(value, f"runtime.{name}")
            if value <= 0:
                raise ValueError(f"runtime.{name} must be > 0")
            object.__setattr__(self, name, float(value))



@dataclass(frozen=True, slots=True)
class SystemSettings:
    """无现场标识的进程配置；风场 ID、名称只保存在 Site。"""

    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    ads: ADSLocalConfig | None = None
