"""把已装配的运行时注入到进程级 AppContext，供 Web API 在测试内解析服务。"""

from __future__ import annotations

from wind_hub_server.application.app_context import AppContext, clear_context, set_context
from wind_hub.application.usecase.task import TaskInstanceDetail
from wind_hub.assembly import AssembledRuntime


def set_runtime_context(rt: AssembledRuntime) -> None:
    """用一次装配的运行时填充 AppContext，使 ``/health`` 等端点返回 200。"""
    set_context(
        AppContext(
            config=rt.config,
            tasks=rt.tasks,
            runtime=rt.runtime,
            command=rt.command,
            query=rt.query,
        )
    )


def clear_runtime_context() -> None:
    """清空上下文，避免测试间泄漏。"""
    clear_context()


async def start_task_instance(rt: AssembledRuntime, instance_id: str) -> TaskInstanceDetail:
    """启动单个采集 Task Instance（如 ``"modbus-telemetry:modbus-1"``）。

    实例在 ``start_runtime`` 后以 STOPPED 注册，必须显式 start 才进入
    周期采集——本辅助函数即测试侧的启动入口（幂等）。
    """
    return await rt.tasks.start_instance(instance_id)


async def start_all_task_instances(rt: AssembledRuntime) -> None:
    """启动全部采集 Task Instance（幂等）。"""
    await rt.tasks.start_all_instances()
