"""Collector and Commander command groups bound to frozen protobuf messages."""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

from core.infrastructure.config import ConfigOperationError, parse_yaml

from .scalar_value import parse_json_scalar
from .service_rpc import invoke, poll_latest, resolve_point_ids

collector = typer.Typer(help="Collector devices, tasks and active PLC measurements.")
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
    interval: float | None = typer.Option(
        None, "--interval", help="Active device reads; frequent polling increases PLC load."
    ),
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
            table = Table(title=f"Collector active read: {device_id}")
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


def _request_receipt(request_id: str, expires_at: str | None = None) -> None:
    """Expose the idempotency key before any control RPC is transmitted."""
    typer.echo(f"Commander request_id={request_id}", err=True)
    if expires_at is not None:
        typer.echo(f"Commander expires_at={expires_at}", err=True)
    sys.stderr.flush()


@commander.command("request-result")
def commander_request_result(request_id: str) -> None:
    """Look up the full caller-scoped result of one submitted request."""
    _call("commander", "GetRequestResult", {"request_id": request_id})


@commander.command("recover-device")
def commander_recover_device(
    device_id: str = typer.Option(..., "--device-id"),
    expected_revision: str = typer.Option(..., "--expected-revision"),
    request_id: str = typer.Option(..., "--request-id"),
    attestation_file: Path = typer.Option(..., "--attestation-file"),
) -> None:
    """Request evidence-gated recovery; never interpret a local file as proof."""
    from .main import _run, runtime

    if not device_id.strip() or not expected_revision.strip() or not request_id.strip():
        raise typer.BadParameter("device-id, expected-revision and request-id must be nonempty")
    evidence = attestation_file.read_bytes()
    if not evidence or len(evidence) > 65536:
        raise typer.BadParameter("attestation file must contain 1..65536 bytes")
    _run(
        "commander recover-device",
        invoke(
            runtime(),
            "commander",
            "RecoverDeviceControl",
            {
                "device_id": device_id,
                "expected_revision": expected_revision,
                "request_id": request_id,
                "isolation_attestation": base64.b64encode(evidence).decode("ascii"),
            },
        ),
    )


@commander.command("write")
def commander_write(
    device_id: str,
    point_id: str,
    value: str,
    yes: bool = typer.Option(False, "--yes"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    request_id: str | None = typer.Option(
        None, "--request-id", help="Reuse an existing recorded idempotency key."
    ),
) -> None:
    """Generate and print the request key before sending any control RPC."""
    from .main import _run, runtime

    def reject(exc: ValueError) -> None:
        runtime().error("commander write", exc)
        raise typer.Exit(code=2) from exc

    try:
        if request_id is not None and not request_id.strip():
            raise ValueError("--request-id must be nonempty")
        if request_id is not None and expires_at is None:
            raise ValueError("--expires-at is required when reusing --request-id")
        if request_id is not None and expected_revision is None:
            raise ValueError("--expected-revision is required when reusing --request-id")
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

    identity = request_id or str(uuid4())

    async def submit() -> dict[str, Any]:
        revision = expected_revision
        if revision is None:
            detail = await invoke(runtime(), "commander", "GetDevice", {"id": device_id})
            device = detail.get("device", {})
            revision = device.get("revision") if isinstance(device, dict) else None
            if not isinstance(revision, str) or not revision:
                raise ValueError("no device revision; pass --expected-revision explicitly")
        _request_receipt(identity, deadline)
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
                "request_id": identity,
            },
        )

    _run("commander write", submit())


@commander.command("batch")
def commander_batch(
    path: Path,
    request_id: str | None = typer.Option(
        None, "--request-id", help="Reuse an existing recorded idempotency key."
    ),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Submit an ordered JSON/YAML command batch with a recorded request ID."""
    from .main import _run, runtime
    from .safety import confirm_mutation

    try:
        if request_id is not None and not request_id.strip():
            raise ValueError("--request-id must be nonempty")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("batch file exceeds 1 MiB")
        source = path.read_text(encoding="utf-8")
        raw = parse_yaml(source) if path.suffix.lower() in {".yaml", ".yml"} else json.loads(source)
        if isinstance(raw, dict):
            if set(raw) != {"commands"}:
                raise ValueError("batch mapping must contain only commands")
            raw = raw["commands"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
            raise ValueError("batch requires 1..1000 ordered commands")
        commands: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or set(item) - {
                "device_id",
                "point_id",
                "value",
                "expected_revision",
                "expires_at",
            }:
                raise ValueError(f"invalid batch command at index {index}")
            if not all(
                isinstance(item.get(key), str) and item[key]
                for key in ("device_id", "point_id", "expected_revision")
            ):
                raise ValueError(f"missing command identity at index {index}")
            command: dict[str, Any] = {
                "device_id": item["device_id"],
                "point_id": item["point_id"],
                "expected_revision": item["expected_revision"],
                "value": parse_json_scalar(json.dumps(item.get("value"), allow_nan=False)),
            }
            if "expires_at" in item:
                expiry = item["expires_at"]
                if not isinstance(expiry, str):
                    raise ValueError(f"expires_at must be a string at index {index}")
                parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError(f"expires_at requires timezone at index {index}")
                command["expires_at"] = expiry
            commands.append(command)
    except (OSError, UnicodeError, ValueError, TypeError, ConfigOperationError) as exc:
        runtime().error("commander batch", exc)
        raise typer.Exit(code=2) from exc

    identity = request_id or str(uuid4())
    confirm_mutation(
        f"Submit {len(commands)} Commander control commands under {identity}?",
        yes,
        command="commander batch",
    )

    async def submit() -> dict[str, Any]:
        _request_receipt(identity)
        return await invoke(
            runtime(),
            "commander",
            "SubmitCommands",
            {"request_id": identity, "commands": commands},
        )

    _run("commander batch", submit())


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
