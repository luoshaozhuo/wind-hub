"""Application context — temporary dependency-injection container.

step8 exposes a process-global :class:`AppContext` so that the CLI and Web API
adapters can resolve their services without holding a hard reference to the
engine assembly.  step9 replaces this with the real composition root
(``main.py``) and wires the concrete services.

The CLI and Web API share this module: the Web API re-exports it from
``wind_hub.adapter.inbound.webapi.context``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wind_hub.domain.port.inbound import (
    CommandUseCase,
    ConfigUseCase,
    JobUseCase,
    QueryUseCase,
    RouteQueryUseCase,
)

if TYPE_CHECKING:
    from wind_hub.application.runtime import Runtime


@dataclass
class AppContext:
    """Application context — temporary dependency-injection container.

    所有服务字段都可选：缺失服务由适配器上报 503 / 非零退出而非崩溃。

    三类启停语义在本容器中各有归属：Runtime 生命周期经 ``runtime``
    （组合根/进程入口编排），Scheduled Job 生命周期经 ``job_service``，
    进程生命周期由 ``main.py`` 信号处理负责——不再有混合语义的
    「task service」。
    """

    command_service: CommandUseCase | None = None
    """可选指令服务。"""

    query_service: QueryUseCase | None = None
    """可选只读查询服务（含系统状态 ``status()``）。"""

    config_service: ConfigUseCase | None = None
    """可选配置服务（热重载）。引擎可能不带配置服务运行（如只读部署），
    缺失时由适配器上报 503 / 非零退出而非崩溃。"""

    router: RouteQueryUseCase | None = None
    """可选路由查询服务（``route explain``）。缺失时同样由适配器兜底。"""

    job_service: JobUseCase | None = None
    """可选调度 Job 管理服务（pause/resume/trigger/status）。"""

    runtime: Runtime | None = None
    """可选 Runtime，供 ``/metrics`` 读取引擎快照（gauge 数据来源）。"""


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


def get_context_or_exit() -> AppContext:
    """Return the context, or print an error and exit(1).

    CLI-only convenience: the API instead maps a missing context to HTTP 503.
    """
    try:
        return get_context()
    except RuntimeError as exc:
        import typer

        from wind_hub.adapter.inbound.cli.output import print_error

        print_error(str(exc))
        raise typer.Exit(1) from exc


__all__ = ["AppContext", "set_context", "get_context", "clear_context", "get_context_or_exit"]
