"""进程级共享 application context——Use Case 与 Runtime 的依赖容器。

``AppContext`` 由组合根（``main.py``）装配后通过 :func:`set_context` 注入，
CLI 与 Web API 两个 inbound adapter 共享同一实例，直接按具体 Use Case
类型读取，而不持有引擎装配的硬引用。

适配层对缺失上下文的处理各自归属：CLI 经
``wind_hub.adapter.inbound.cli.context.get_context_or_exit`` 报错退出，
Web API 经 ``wind_hub.adapter.inbound.webapi.context.get_ctx`` 映射为
HTTP 503。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from wind_hub.application.operation import OperationManager
from wind_hub.application.runtime import Runtime
from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.device import DeviceUseCase
from wind_hub.application.usecase.device_control import DeviceControlUseCase
from wind_hub.application.usecase.device_data import DeviceDataUseCase
from wind_hub.application.usecase.overview import OverviewUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub.application.usecase.task import TaskUseCase


@dataclass
class AppContext:
    """进程级共享 application context——Use Case 与 Runtime 的依赖容器。

    所有字段都可选：缺失用例由适配器上报 503 / 非零退出而非崩溃。

    三类启停语义在本容器中各有归属：Runtime 生命周期经 ``runtime``
    （组合根/进程入口编排），采集 Task Instance 生命周期经 ``tasks``，
    进程生命周期由 ``main.py`` 信号处理负责。
    """

    command: CommandUseCase | None = None
    """可选指令下发用例。"""

    query: QueryUseCase | None = None
    """可选只读查询用例（含系统状态 ``status()``）。"""

    config: ConfigUseCase | None = None
    """可选配置用例（热重载）。引擎可能不带配置用例运行（如只读部署），
    缺失时由适配器上报 503 / 非零退出而非崩溃。"""

    tasks: TaskUseCase | None = None
    """可选采集 Task 生命周期用例（查询 / start / stop / start-all / stop-all）。"""

    runtime: Runtime | None = None
    """可选 Runtime，供 /metrics 读取引擎快照。"""

    devices: DeviceUseCase | None = None
    """V1 设备查询用例；聚合静态配置与实时连接状态。"""

    device_data: DeviceDataUseCase | None = None
    """V1 Devices Data / Trend 缓存查询用例。"""

    device_control: DeviceControlUseCase | None = None
    """V1 设备写控制与回读用例。"""

    overview: OverviewUseCase | None = None
    """V1 Overview 聚合只读模型。"""

    operations: OperationManager | None = None
    """进程内异步 Operation 注册表。"""


_context: AppContext | None = None
_lock = threading.Lock()


def set_context(ctx: AppContext) -> None:
    """Set the global application context (called by ``main.py`` or tests)."""
    global _context
    with _lock:
        _context = ctx


def get_context() -> AppContext:
    """Return the global application context.

    Raises:
        RuntimeError: If no context has been set yet.
    """
    with _lock:
        ctx = _context
    if ctx is None:
        raise RuntimeError("AppContext is not set — call set_context() first")
    return ctx


def clear_context() -> None:
    """Clear the global context (used by tests for isolation)."""
    global _context
    with _lock:
        _context = None


__all__ = ["AppContext", "set_context", "get_context", "clear_context"]
