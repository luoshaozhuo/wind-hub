"""``wind-hub jobs`` — 采集 Job 生命周期管理（查询 / start / stop / 批量启停）。

命令只经 ``jobs``（:class:`~wind_hub.application.usecase.job.JobUseCase`）
操作调度状态：start/stop 是 resume/pause 语义，不增删 Job、不触碰设备连接。
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_kv, print_table
from wind_hub.application.usecase.job import JobBatchResult, JobDetail, JobUseCase

app = typer.Typer(name="jobs", help="采集任务管理")

_COLUMNS = ["job_id", "device", "group", "interval", "state", "next_run"]


def _service() -> JobUseCase:
    ctx = get_context_or_exit()
    if ctx.jobs is None:
        print_error("Job 用例未配置（AppContext.jobs 为 None）")
        raise typer.Exit(1)
    return ctx.jobs


def _row(job: JobDetail) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "device": job.device_id,
        "group": job.group,
        "interval": job.interval_seconds,
        "state": job.state.value,
        "next_run": job.next_run_time,
    }


def _print_job(job: JobDetail, as_json: bool) -> None:
    if as_json:
        print_json(job.model_dump(mode="json"))
    else:
        print_table([_row(job)], _COLUMNS)


def _print_batch(result: JobBatchResult, action: str, as_json: bool) -> None:
    if as_json:
        print_json(result.model_dump(mode="json"))
    else:
        print_kv({action: result.changed, "unchanged": result.unchanged, "total": result.total})


@app.command("list")
def list_jobs(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """列出全部采集 Job 及其调度状态。"""
    asyncio.run(_list(json))


async def _list(as_json: bool) -> None:
    jobs = await _service().list_jobs()
    if as_json:
        print_json([j.model_dump(mode="json") for j in jobs])
    else:
        print_table([_row(j) for j in jobs], _COLUMNS)


@app.command("show")
def show(
    job_id: str = typer.Argument(..., help="Job ID（如 poll:wtg-001:fast）"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """查看单个 Job 的调度状态；不存在时退出码 1。"""
    asyncio.run(_show(job_id, json))


async def _show(job_id: str, as_json: bool) -> None:
    try:
        job = await _service().get_job(job_id)
    except KeyError:
        print_error(f"任务不存在：{job_id}")
        raise typer.Exit(1) from None
    _print_job(job, as_json)


@app.command("start")
def start(
    job_id: str = typer.Argument(..., help="Job ID"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """启动单个 Job 的周期调度（幂等；不立即执行额外采集）。"""
    asyncio.run(_set_one(job_id, start=True, as_json=json))


@app.command("stop")
def stop(
    job_id: str = typer.Argument(..., help="Job ID"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """停止单个 Job 的周期调度（幂等；不删除 Job、不断开设备连接）。"""
    asyncio.run(_set_one(job_id, start=False, as_json=json))


async def _set_one(job_id: str, *, start: bool, as_json: bool) -> None:
    service = _service()
    try:
        job = await (service.start_job(job_id) if start else service.stop_job(job_id))
    except KeyError:
        print_error(f"任务不存在：{job_id}")
        raise typer.Exit(1) from None
    _print_job(job, as_json)


@app.command("start-all")
def start_all(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """启动全部采集 Job（已 RUNNING 的保持不变）。"""
    asyncio.run(_set_all(start=True, as_json=json))


@app.command("stop-all")
def stop_all(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """停止全部采集 Job 的周期调度（不影响 Runtime 与设备连接）。"""
    asyncio.run(_set_all(start=False, as_json=json))


async def _set_all(*, start: bool, as_json: bool) -> None:
    service = _service()
    result = await (service.start_all_jobs() if start else service.stop_all_jobs())
    _print_batch(result, "started" if start else "stopped", as_json)
