"""Storage Service CLI: bounded read, export/backup, and partition recovery."""

from __future__ import annotations

from typing import Any

import typer

from cli.service_rpc import invoke

storage = typer.Typer(help="Storage historical queries and recovery operations.")


def _invoke(method: str, fields: dict[str, Any]) -> None:
    from cli.main import _run, runtime

    async def task() -> dict[str, Any]:
        return await invoke(runtime(), "storage", method, fields)

    _run(f"storage {method}", task())


def _filter(
    farm: str,
    level: str,
    start_time: str,
    end_time: str,
    device_type: str,
    device_id: str,
    point_table_id: str,
    point_id: str,
    business_point_id: str,
) -> dict[str, Any]:
    levels = {
        "raw": "STORAGE_DATA_LEVEL_RAW",
        "protocol": "STORAGE_DATA_LEVEL_PROTOCOL",
        "business": "STORAGE_DATA_LEVEL_BUSINESS",
    }
    if level not in levels:
        raise typer.BadParameter("level must be raw, protocol, or business")
    if not farm:
        raise typer.BadParameter("wind-farm-id must be nonempty")
    return {
        "wind_farm_id": farm,
        "data_level": levels[level],
        "start_time": start_time,
        "end_time": end_time,
        "device_type": device_type,
        "device_id": device_id,
        "point_table_id": point_table_id,
        "point_id": point_id,
        "business_point_id": business_point_id,
    }


@storage.command("wide-rows")
def query_wide_rows(
    wind_farm_id: str,
    device_id: str,
    device_config_id: str,
    start_time: str,
    end_time: str,
    level: str = typer.Option(..., "--level"),
    point_ids: list[str] | None = typer.Option(None, "--point-id"),
    page_size: int = typer.Option(200, min=1, max=1000),
    page_token: str = "",
) -> None:
    """Read one exact device/schema family as timestamped wide rows."""
    levels = {
        "raw": "STORAGE_DATA_LEVEL_RAW",
        "protocol": "STORAGE_DATA_LEVEL_PROTOCOL",
        "business": "STORAGE_DATA_LEVEL_BUSINESS",
    }
    if level not in levels:
        raise typer.BadParameter("level must be raw, protocol, or business")
    if not all((wind_farm_id, device_id, device_config_id, start_time, end_time)):
        raise typer.BadParameter("wide query requires complete device identity and UTC range")
    _invoke(
        "QueryWideRows",
        {
            "wind_farm_id": wind_farm_id,
            "device_id": device_id,
            "device_config_id": device_config_id,
            "data_level": levels[level],
            "start_time": start_time,
            "end_time": end_time,
            "point_ids": point_ids or [],
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@storage.command("samples")
def query_samples(
    wind_farm_id: str,
    start_time: str,
    end_time: str,
    level: str = typer.Option(..., "--level"),
    device_type: str = "",
    device_id: str = "",
    point_table_id: str = "",
    point_id: str = "",
    business_point_id: str = "",
    page_size: int = typer.Option(200, min=1, max=1000),
    page_token: str = "",
) -> None:
    """Query an explicit UTC half-open time window."""
    _invoke(
        "QuerySamples",
        {
            "filter": _filter(
                wind_farm_id,
                level,
                start_time,
                end_time,
                device_type,
                device_id,
                point_table_id,
                point_id,
                business_point_id,
            ),
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@storage.command("wide-aggregates")
def query_wide_aggregates(
    wind_farm_id: str,
    device_id: str,
    device_config_id: str,
    start_time: str,
    end_time: str,
    level: str = typer.Option(..., "--level"),
    operation: str = typer.Option("avg", "--operation"),
    window_ms: int = typer.Option(60000, min=1),
    point_ids: list[str] | None = typer.Option(None, "--point-id"),
    page_size: int = typer.Option(200, min=1, max=1000),
    page_token: str = "",
) -> None:
    """Aggregate one immutable wide device/schema family with precise integer results."""
    levels = {
        "raw": "STORAGE_DATA_LEVEL_RAW",
        "protocol": "STORAGE_DATA_LEVEL_PROTOCOL",
        "business": "STORAGE_DATA_LEVEL_BUSINESS",
    }
    operations = {
        "count": "STORAGE_AGGREGATION_COUNT",
        "min": "STORAGE_AGGREGATION_MIN",
        "max": "STORAGE_AGGREGATION_MAX",
        "avg": "STORAGE_AGGREGATION_AVG",
    }
    if level not in levels or operation not in operations:
        raise typer.BadParameter("invalid data_level or aggregation operation")
    _invoke(
        "QueryAggregates",
        {
            "wide_filter": {
                "wind_farm_id": wind_farm_id,
                "device_id": device_id,
                "device_config_id": device_config_id,
                "data_level": levels[level],
                "start_time": start_time,
                "end_time": end_time,
                "point_ids": point_ids or [],
                "page_size": min(page_size, 200),
            },
            "aggregation": operations[operation],
            "window_ms": window_ms,
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@storage.command("aggregates")
def query_aggregates(
    wind_farm_id: str,
    start_time: str,
    end_time: str,
    level: str = typer.Option(..., "--level"),
    operation: str = typer.Option("avg", "--operation"),
    window_ms: int = typer.Option(60000, min=1),
    point_id: str = "",
    business_point_id: str = "",
    point_table_id: str = "",
    device_type: str = "",
    device_id: str = "",
    page_size: int = typer.Option(200, min=1, max=1000),
    page_token: str = "",
) -> None:
    choices = {
        "count": "STORAGE_AGGREGATION_COUNT",
        "min": "STORAGE_AGGREGATION_MIN",
        "max": "STORAGE_AGGREGATION_MAX",
        "avg": "STORAGE_AGGREGATION_AVG",
    }
    if operation not in choices:
        raise typer.BadParameter("operation must be count, min, max, or avg")
    _invoke(
        "QueryAggregates",
        {
            "filter": _filter(
                wind_farm_id,
                level,
                start_time,
                end_time,
                device_type,
                device_id,
                point_table_id,
                point_id,
                business_point_id,
            ),
            "aggregation": choices[operation],
            "window_ms": window_ms,
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@storage.command("wide-export")
def create_wide_export(
    request_id: str,
    wind_farm_id: str,
    device_id: str,
    device_config_id: str,
    start_time: str,
    end_time: str,
    level: str = typer.Option(..., "--level"),
    export_format: str = typer.Option("csv", "--format"),
    point_ids: list[str] | None = typer.Option(None, "--point-id"),
    page_size: int = typer.Option(100, min=1, max=1000),
) -> None:
    """Create a durable version-scoped device-wide CSV/JSON export task."""
    levels = {
        "raw": "STORAGE_DATA_LEVEL_RAW",
        "protocol": "STORAGE_DATA_LEVEL_PROTOCOL",
        "business": "STORAGE_DATA_LEVEL_BUSINESS",
    }
    if level not in levels:
        raise typer.BadParameter("invalid data_level")
    if export_format not in {"csv", "json"}:
        raise typer.BadParameter("wide export format must be csv or json")
    _invoke(
        "CreateExport",
        {
            "request_id": request_id,
            "export_format": export_format,
            "wide_filter": {
                "wind_farm_id": wind_farm_id,
                "device_id": device_id,
                "device_config_id": device_config_id,
                "data_level": levels[level],
                "start_time": start_time,
                "end_time": end_time,
                "point_ids": point_ids or [],
                "page_size": page_size,
            },
        },
    )


@storage.command("export")
def create_export(
    request_id: str,
    wind_farm_id: str,
    start_time: str,
    end_time: str,
    level: str = typer.Option(..., "--level"),
    point_id: str = "",
    business_point_id: str = "",
    point_table_id: str = "",
    device_type: str = "",
    device_id: str = "",
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from cli.safety import confirm_mutation

    confirm_mutation("Create Storage export job?", yes, command="storage export")
    _invoke(
        "CreateExport",
        {
            "request_id": request_id,
            "export_format": "csv",
            "filter": _filter(
                wind_farm_id,
                level,
                start_time,
                end_time,
                device_type,
                device_id,
                point_table_id,
                point_id,
                business_point_id,
            ),
        },
    )


@storage.command("export-status")
def export_status(task_id: str) -> None:
    _invoke("GetExportTask", {"task_id": task_id})


@storage.command("backup")
def create_backup(
    request_id: str,
    wind_farm_id: str,
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from cli.safety import confirm_mutation

    confirm_mutation("Create Storage backup job?", yes, command="storage backup")
    _invoke("CreateBackup", {"request_id": request_id, "wind_farm_id": wind_farm_id})


@storage.command("backup-status")
def backup_status(task_id: str) -> None:
    _invoke("GetBackupTask", {"task_id": task_id})


@storage.command("stats")
def get_stats(wind_farm_id: str = "") -> None:
    _invoke("GetStorageStats", {"wind_farm_id": wind_farm_id})


@storage.command("blocked")
def list_blocked(
    page_size: int = typer.Option(100, min=1, max=1000),
    page_token: str = "",
) -> None:
    _invoke("ListBlockedPartitions", {"page_size": page_size, "page_token": page_token})


@storage.command("retry")
def retry_blocked(
    request_id: str,
    topic: str,
    partition: int = typer.Option(..., min=0),
    expected_blocked_offset: int = typer.Option(..., min=0),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from cli.safety import confirm_mutation

    confirm_mutation(
        f"Retry Storage {topic}/{partition} at offset {expected_blocked_offset}?",
        yes,
        command="storage retry",
    )
    _invoke(
        "RetryBlockedPartition",
        {
            "request_id": request_id,
            "topic": topic,
            "partition": partition,
            "expected_blocked_offset": str(expected_blocked_offset),
        },
    )
