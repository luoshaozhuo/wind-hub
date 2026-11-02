"""Collector and Commander command groups bound to frozen protobuf messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import typer

from .scalar_value import parse_json_scalar
from .service_rpc import invoke, poll_latest, resolve_point_ids

collector = typer.Typer(help="Collector devices, tasks and cached measurements.")
commander = typer.Typer(help="Commander configuration and control.")


def _call(service: str, method: str, fields: dict[str, Any]) -> None:
    from .main import _run, runtime

    _run(f"{service} {method}", invoke(runtime(), service, method, fields))


@collector.command("devices")
def collector_devices(filter: str = "", page_size: int = 100, page_token: str = "") -> None:
    _call(
        "collector",
        "ListDevices",
        {"filter": filter, "page_size": page_size, "page_token": page_token},
    )


@collector.command("device")
def collector_device(device_id: str) -> None:
    _call("collector", "GetDevice", {"device_id": device_id})


@collector.command("points")
def collector_points(device_id: str, page_size: int = 100, page_token: str = "") -> None:
    _call(
        "collector",
        "ListPoints",
        {"device_id": device_id, "page_size": page_size, "page_token": page_token},
    )


@collector.command("point")
def collector_point(device_id: str, point_id: str) -> None:
    _call("collector", "GetPoint", {"device_id": device_id, "point_id": point_id})


@collector.command("tasks")
def collector_tasks(page_size: int = 100, page_token: str = "") -> None:
    _call("collector", "ListTasks", {"page_size": page_size, "page_token": page_token})


@collector.command("task")
def collector_task(task_id: str) -> None:
    _call("collector", "GetTask", {"id": task_id})


async def _task_action(task_id: str, operation: str, revision: str | None) -> dict[str, Any]:
    from .main import runtime

    if revision is None:
        response = await invoke(runtime(), "collector", "GetTask", {"id": task_id})
        task = response.get("task", {})
        revision = task.get("revision") if isinstance(task, dict) else None
    if not isinstance(revision, str) or not revision:
        raise ValueError("task revision unavailable; pass --expected-revision")
    return await invoke(
        runtime(),
        "collector",
        operation,
        {
            "id": task_id,
            "expected_revision": revision,
        },
    )


@collector.command("start")
def collector_start(
    task_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(f"Start collector task {task_id}?", yes, command="collector start")
    _run("collector start", _task_action(task_id, "StartTask", expected_revision))


@collector.command("stop")
def collector_stop(
    task_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(f"Stop collector task {task_id}?", yes, command="collector stop")
    _run("collector stop", _task_action(task_id, "StopTask", expected_revision))


@collector.command("latest")
def collector_latest(
    device_id: str,
    point_ids: list[str] | None = typer.Argument(None),
) -> None:
    from .main import _run, runtime

    async def read() -> dict[str, Any]:
        selected = await resolve_point_ids(runtime(), device_id, point_ids)
        return await invoke(
            runtime(),
            "collector",
            "ReadPoints",
            {
                "device_id": device_id,
                "point_ids": selected,
            },
        )

    _run("collector latest", read())


@collector.command("publishing")
def collector_publishing(
    channel: Annotated[str, typer.Argument()] = "",
) -> None:
    # 设计 9.3：channel 为可选筛选；位置参数缺省即查询全部通道。
    _call("collector", "GetPublishingStatus", {"channel": channel})


@collector.command("watch")
def collector_watch(
    device_id: str,
    point_ids: list[str] | None = typer.Argument(None),
    interval: float | None = typer.Option(None, "--interval"),
    count: int | None = typer.Option(None, "--count"),
) -> None:
    from rich.live import Live
    from rich.table import Table

    from .main import _run, runtime

    async def collect() -> dict[str, Any]:
        ctx = runtime()
        settings = ctx.config()
        seconds = interval if interval is not None else settings.poll_interval_ms / 1000
        fmt = ctx.output_format or settings.output_format
        iterations = 0

        def make_table(sample: dict[str, Any]) -> Table:
            table = Table(title=f"Collector cache: {device_id}")
            for column in ("Point", "Value", "Quality", "Freshness", "Timestamp"):
                table.add_column(column)
            for item in sample.get("samples", []):
                if not isinstance(item, dict):
                    continue
                table.add_row(
                    str(item.get("point_id", "")),
                    str(item.get("value", "<no value>")),
                    str(item.get("quality", "")),
                    str(item.get("freshness", "")),
                    str(item.get("sample_timestamp", "")),
                )
            return table

        if fmt == "json":
            async for sample in poll_latest(ctx, device_id, point_ids, seconds, count):
                ctx.present("collector watch", sample)
                iterations += 1
        else:
            with Live(console=ctx.console, refresh_per_second=4, transient=True) as live:
                async for sample in poll_latest(ctx, device_id, point_ids, seconds, count):
                    live.update(make_table(sample))
                    iterations += 1
        return {"device_id": device_id, "iterations": iterations}

    _run("collector watch", collect())


@commander.command("devices")
def commander_devices(page_size: int = 100, page_token: str = "") -> None:
    _call("commander", "ListDevices", {"page_size": page_size, "page_token": page_token})


@commander.command("device")
def commander_device(device_id: str) -> None:
    _call("commander", "GetDevice", {"id": device_id})


@commander.command("points")
def commander_points(device_id: str, page_size: int = 100, page_token: str = "") -> None:
    _call(
        "commander",
        "ListPoints",
        {"device_id": device_id, "page_size": page_size, "page_token": page_token},
    )


@commander.command("result")
def commander_result(command_id: str) -> None:
    _call("commander", "GetCommandResult", {"command_id": command_id})


@commander.command("write")
def commander_write(
    device_id: str,
    point_id: str,
    value: str,
    yes: bool = typer.Option(False, "--yes"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
) -> None:
    """Confirm and submit a single control command; never retry ambiguous writes."""
    from .main import _run, runtime

    def reject(exc: ValueError) -> None:
        runtime().error("commander write", exc)
        raise typer.Exit(code=2) from exc

    try:
        scalar = parse_json_scalar(value)
        deadline = expires_at or (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed <= datetime.now(UTC):
            raise ValueError("expires_at must be a future timezone-aware timestamp")
    except ValueError as exc:
        reject(exc)

    if not yes:
        if runtime().output_format == "json":
            reject(ValueError("--yes required for non-interactive JSON write"))
        typer.echo(f"Target: {device_id} / {point_id}")
        typer.echo(f"Value: {value}, expires_at: {deadline}")
        if not typer.confirm("Confirm control command?", default=False):
            raise typer.Abort()

    async def submit() -> dict[str, Any]:
        revision = expected_revision
        if revision is None:
            detail = await invoke(runtime(), "commander", "GetDevice", {"id": device_id})
            device = detail.get("device", {})
            revision = device.get("revision") if isinstance(device, dict) else None
            if not isinstance(revision, str) or not revision:
                raise ValueError("no device revision; pass --expected-revision explicitly")
        return await invoke(
            runtime(),
            "commander",
            "SubmitCommand",
            {
                "device_id": device_id,
                "point_id": point_id,
                "value": scalar,
                "expires_at": deadline,
                "expected_revision": revision,
            },
        )

    _run("commander write", submit())


@collector.command("metrics")
def collector_metrics(scope: str = "", window_ms: int = 60000) -> None:
    from .common_commands import _run_common

    _run_common("collector", "GetMetrics", {"scope": scope, "window_ms": window_ms})


@collector.command("events")
def collector_events(page_size: int = 100, page_token: str = "") -> None:
    from .common_commands import _run_common

    _run_common(
        "collector",
        "GetEvents",
        {
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@commander.command("metrics")
def commander_metrics(scope: str = "", window_ms: int = 60000) -> None:
    from .common_commands import _run_common

    _run_common("commander", "GetMetrics", {"scope": scope, "window_ms": window_ms})


@commander.command("events")
def commander_events(page_size: int = 100, page_token: str = "") -> None:
    from .common_commands import _run_common

    _run_common(
        "commander",
        "GetEvents",
        {
            "page_size": page_size,
            "page_token": page_token,
        },
    )
