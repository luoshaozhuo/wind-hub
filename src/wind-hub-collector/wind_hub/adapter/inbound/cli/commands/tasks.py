"""``wind-hub tasks`` — 采集 Task / Task Instance 生命周期管理。

命令只经 ``tasks``（:class:`~wind_hub.application.usecase.task.TaskUseCase`）
操作实例生命周期：start/stop 翻转 RUNNING/STOPPED 状态，不增删实例、
不触碰设备连接。
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_kv, print_table
from wind_hub.application.usecase.task import (
    TaskBatchResult,
    TaskDetail,
    TaskInstanceDetail,
    TaskUseCase,
)

app = typer.Typer(name="tasks", help="采集任务管理")

_TASK_COLUMNS = [
    "task_id",
    "device",
    "device_group",
    "point_group",
    "interval",
    "targets",
    "enabled",
]
_INSTANCE_COLUMNS = ["instance_id", "task_id", "device", "point_group", "interval", "state"]


def _service() -> TaskUseCase:
    ctx = get_context_or_exit()
    if ctx.tasks is None:
        print_error("Task 用例未配置（AppContext.tasks 为 None）")
        raise typer.Exit(1)
    return ctx.tasks


def _task_row(task: TaskDetail) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "device": task.device,
        "device_group": task.device_group,
        "point_group": task.point_group,
        "interval": task.interval,
        "targets": task.targets,
        "enabled": task.enabled,
    }


def _instance_row(inst: TaskInstanceDetail) -> dict[str, Any]:
    return {
        "instance_id": inst.instance_id,
        "task_id": inst.task_id,
        "device": inst.device_id,
        "point_group": inst.point_group,
        "interval": inst.interval,
        "state": inst.state.value,
    }


def _print_instance(inst: TaskInstanceDetail, as_json: bool) -> None:
    if as_json:
        print_json(inst.model_dump(mode="json"))
    else:
        print_table([_instance_row(inst)], _INSTANCE_COLUMNS)


def _print_batch(result: TaskBatchResult, action: str, as_json: bool) -> None:
    if as_json:
        print_json(result.model_dump(mode="json"))
    else:
        print_kv({action: result.changed, "unchanged": result.unchanged, "total": result.total})


@app.command("list")
def list_tasks(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """列出全部采集 Task 定义（tasks.yaml）。"""
    asyncio.run(_list_tasks(json))


async def _list_tasks(as_json: bool) -> None:
    tasks = await _service().list_tasks()
    if as_json:
        print_json([t.model_dump(mode="json") for t in tasks])
    else:
        print_table([_task_row(t) for t in tasks], _TASK_COLUMNS)


@app.command("instances")
def list_instances(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """列出全部 Task Instance 及其生命周期状态。"""
    asyncio.run(_list_instances(json))


async def _list_instances(as_json: bool) -> None:
    instances = await _service().list_instances()
    if as_json:
        print_json([i.model_dump(mode="json") for i in instances])
    else:
        print_table([_instance_row(i) for i in instances], _INSTANCE_COLUMNS)


@app.command("show")
def show(
    instance_id: str = typer.Argument(..., help="实例 ID（如 turbine-fast:wtg-003）"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """查看单个 Task Instance 的状态；不存在时退出码 1。"""
    asyncio.run(_show(instance_id, json))


async def _show(instance_id: str, as_json: bool) -> None:
    try:
        inst = await _service().get_instance(instance_id)
    except KeyError:
        print_error(f"实例不存在：{instance_id}")
        raise typer.Exit(1) from None
    _print_instance(inst, as_json)


@app.command("start")
def start(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """启动单个实例的周期采集（幂等）。"""
    asyncio.run(_set_one(instance_id, start=True, as_json=json))


@app.command("stop")
def stop(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """停止单个实例的周期采集（幂等；不删除实例、不断开设备连接）。"""
    asyncio.run(_set_one(instance_id, start=False, as_json=json))


async def _set_one(instance_id: str, *, start: bool, as_json: bool) -> None:
    service = _service()
    try:
        inst = await (
            service.start_instance(instance_id) if start else service.stop_instance(instance_id)
        )
    except KeyError:
        print_error(f"实例不存在：{instance_id}")
        raise typer.Exit(1) from None
    _print_instance(inst, as_json)


@app.command("start-all")
def start_all(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """启动全部 Task Instance（已 RUNNING 的保持不变）。"""
    asyncio.run(_set_all(start=True, as_json=json))


@app.command("stop-all")
def stop_all(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """停止全部 Task Instance 的周期采集（不影响 Runtime 与设备连接）。"""
    asyncio.run(_set_all(start=False, as_json=json))


async def _set_all(*, start: bool, as_json: bool) -> None:
    service = _service()
    result = await (service.start_all_instances() if start else service.stop_all_instances())
    _print_batch(result, "started" if start else "stopped", as_json)
