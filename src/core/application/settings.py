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
    """system.yaml ``ads`` 段的完整契约：本机 AMS 路由器身份（restart-required）。

    远程 PLC 的路由注册凭据（username/password）属于设备/型号级
    ``protocol_options``，不放在本机身份里。
    """

    local_ams_net_id: str
    local_ip: str
    request_timeout_ms: int = 1000
    connect_timeout_ms: int = 3000
    close_timeout_ms: int = 3000

    def __post_init__(self) -> None:
        _require_non_empty(self.local_ams_net_id, "ads.local_ams_net_id")
        _require_non_empty(self.local_ip, "ads.local_ip")
        for name in ("request_timeout_ms", "connect_timeout_ms", "close_timeout_ms"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"ads.{name} must be a positive integer")


# 进程级同步命令线程池上限：默认 32；ADS 无锁实验证明不能假设 AdsLib
# 28~32 的固定上限（每 Session 仍严格串行），但 64/128 不应成为默认值。
_THREAD_POOL_MIN_WORKERS = 1
_THREAD_POOL_MAX_WORKERS = 256
_THREAD_POOL_DEFAULT_WORKERS = 32


@dataclass(frozen=True, slots=True)
class ThreadPoolSettings:
    """system.yaml ``runtime.thread_pool`` 段（restart-required）。

    进程级唯一的同步命令线程池（``loop.set_default_executor``），供所有
    协议 Driver 的 ``asyncio.to_thread`` 共用；Driver 不得自建/销毁线程池。
    """

    max_workers: int = _THREAD_POOL_DEFAULT_WORKERS

    def __post_init__(self) -> None:
        if isinstance(self.max_workers, bool) or not isinstance(self.max_workers, int):
            raise ValueError("runtime.thread_pool.max_workers must be an integer")
        if not _THREAD_POOL_MIN_WORKERS <= self.max_workers <= _THREAD_POOL_MAX_WORKERS:
            raise ValueError(
                "runtime.thread_pool.max_workers must be in "
                f"{_THREAD_POOL_MIN_WORKERS}..{_THREAD_POOL_MAX_WORKERS}"
            )


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """跨进程共享的运行参数；``None`` 表示未配置，默认值由各进程自定。"""

    queue_maxsize: int | None = None
    backpressure_policy: str | None = None
    shutdown_timeout: float | None = None
    connect_timeout: float | None = None
    read_timeout: float | None = None
    write_timeout: float | None = None
    watchdog_interval: float = 5.0
    health_timeout: float = 1.0
    # None 表示 system.yaml 未配置 runtime.thread_pool；进程引导时回退到
    # ThreadPoolSettings 的默认 max_workers=32。保留“未配置”语义以保证
    # 配置保存的无损往返（不为缺省配置凭空写回该段）。
    thread_pool: ThreadPoolSettings | None = None

    def __post_init__(self) -> None:
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
        for name in (
            "shutdown_timeout",
            "connect_timeout",
            "read_timeout",
            "write_timeout",
            "watchdog_interval",
            "health_timeout",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            _require_finite(value, f"runtime.{name}")
            if value <= 0:
                raise ValueError(f"runtime.{name} must be > 0")
            object.__setattr__(self, name, float(value))


@dataclass(frozen=True, slots=True)
class LogSettings:
    """跨进程日志策略；日志处理器由各进程初始化，文件必须独占。"""

    level: str = "INFO"
    path: str = "/var/tmp/wind-hub/logs"
    console: bool = True
    max_size_mb: float = 100.0
    retention_days: int = 30

    def __post_init__(self) -> None:
        if self.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("logs.level must be a standard uppercase logging level")
        _require_non_empty(self.path, "logs.path")
        if not isinstance(self.console, bool):
            raise ValueError("logs.console must be boolean")
        _require_finite(self.max_size_mb, "logs.max_size_mb")
        if self.max_size_mb <= 0:
            raise ValueError("logs.max_size_mb must be > 0")
        if (
            isinstance(self.retention_days, bool)
            or not isinstance(self.retention_days, int)
            or self.retention_days < 1
        ):
            raise ValueError("logs.retention_days must be a positive integer")


@dataclass(frozen=True, slots=True)
class SystemSettings:
    """无现场标识的进程配置；风场 ID、名称只保存在 Site。"""

    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    ads: ADSLocalConfig | None = None
    logs: LogSettings = field(default_factory=LogSettings)
