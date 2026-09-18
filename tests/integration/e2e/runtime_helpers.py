"""把已装配的运行时注入到进程级 AppContext，供 Web API 在测试内解析服务。"""

from __future__ import annotations

from wind_hub.adapter.inbound.cli.context import AppContext, clear_context, set_context
from wind_hub.assembly import AssembledRuntime


def set_runtime_context(rt: AssembledRuntime) -> None:
    """用一次装配的运行时填充 AppContext，使 ``/health`` 等端点返回 200。"""
    set_context(
        AppContext(
            config_service=rt.config_service,
            router=rt.route_query_service,
            job_service=rt.job_service,
            runtime=rt.runtime,
            command_service=rt.command_service,
            query_service=rt.query_service,
        )
    )


def clear_runtime_context() -> None:
    """清空上下文，避免测试间泄漏。"""
    clear_context()
