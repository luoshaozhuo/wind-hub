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


_LEVELS = {
    "raw": "STORAGE_DATA_LEVEL_RAW",
    "protocol": "STORAGE_DATA_LEVEL_PROTOCOL",
    "business": "STORAGE_DATA_LEVEL_BUSINESS",
}


def _history_filter(
    *,
    wind_farm_id: str,
    start_time: str,
    end_time: str,
    level: str,
    device_ids: list[str] | None,
    device_type: str,
    point_table_id: str,
    point_ids: list[str] | None,
    qualities: list[int] | None,
    schema_fingerprint: str,
) -> dict[str, Any]:
    """Construct only the frozen v2 grouped-history filter fields."""
    if level not in _LEVELS:
        raise typer.BadParameter("level must be raw, protocol, or business")
    if not all((wind_farm_id, start_time, end_time)):
        raise typer.BadParameter("history requires farm and UTC time boundaries")
    if qualities and (
        len(set(qualities)) != len(qualities) or any(not 0 <= quality <= 3 for quality in qualities)
    ):
        raise typer.BadParameter("quality must be one of 0, 1, 2, 3 without duplicates")
    fields: dict[str, Any] = {
        "wind_farm_id": wind_farm_id,
        "device_type": device_type,
        "device_ids": device_ids or [],
        "point_table_id": point_table_id,
        "data_level": _LEVELS[level],
        "point_ids": point_ids or [],
        "start_time": start_time,
        "end_time": end_time,
        "qualities": qualities or [],
    }
    if schema_fingerprint:
        fields["schema_fingerprint"] = schema_fingerprint
    return fields


@storage.command("samples")
def query_samples(
    wind_farm_id: str = typer.Option(..., "--wind-farm-id"),
    start_time: str = typer.Option(..., "--start-time"),
    end_time: str = typer.Option(..., "--end-time"),
    level: str = typer.Option(..., "--data-level"),
    device_ids: list[str] | None = typer.Option(None, "--device-id"),
    device_type: str = typer.Option("", "--device-type"),
    point_table_id: str = typer.Option("", "--point-table-id"),
    point_ids: list[str] | None = typer.Option(None, "--point-id"),
    qualities: list[int] | None = typer.Option(None, "--quality"),
    schema_fingerprint: str = typer.Option("", "--schema-fingerprint"),
    page_size: int = typer.Option(200, min=1, max=1000),
    page_token: str = "",
) -> None:
    """Query grouped device-wide rows across immutable physical schemas."""
    _invoke(
        "QuerySamples",
        {
            "filter": _history_filter(
                wind_farm_id=wind_farm_id,
                start_time=start_time,
                end_time=end_time,
                level=level,
                device_ids=device_ids,
                device_type=device_type,
                point_table_id=point_table_id,
                point_ids=point_ids,
                qualities=qualities,
                schema_fingerprint=schema_fingerprint,
            ),
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
    operation: str = typer.Option("avg", "--aggregation"),
    window_ms: int = typer.Option(60000, min=1),
    device_ids: list[str] | None = typer.Option(None, "--device-id"),
    device_type: str = typer.Option("", "--device-type"),
    point_table_id: str = typer.Option("", "--point-table-id"),
    point_ids: list[str] | None = typer.Option(None, "--point-id"),
    qualities: list[int] | None = typer.Option(None, "--quality"),
    schema_fingerprint: str = typer.Option("", "--schema-fingerprint"),
    page_size: int = typer.Option(200, min=1, max=1000),
    page_token: str = "",
) -> None:
    """Aggregate independently by device, immutable schema and time window."""
    operations = {
        "count": "STORAGE_AGGREGATION_COUNT",
        "min": "STORAGE_AGGREGATION_MIN",
        "max": "STORAGE_AGGREGATION_MAX",
        "avg": "STORAGE_AGGREGATION_AVG",
    }
    if operation not in operations:
        raise typer.BadParameter("operation must be count, min, max, or avg")
    _invoke(
        "QueryAggregates",
        {
            "filter": _history_filter(
                wind_farm_id=wind_farm_id,
                start_time=start_time,
                end_time=end_time,
                level=level,
                device_ids=device_ids,
                device_type=device_type,
                point_table_id=point_table_id,
                point_ids=point_ids,
                qualities=qualities,
                schema_fingerprint=schema_fingerprint,
            ),
            "aggregation": operations[operation],
            "window_ms": window_ms,
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@storage.command("export")
def create_export(
    request_id: str,
    wind_farm_id: str = typer.Option(..., "--wind-farm-id"),
    start_time: str = typer.Option(..., "--start-time"),
    end_time: str = typer.Option(..., "--end-time"),
    level: str = typer.Option(..., "--data-level"),
    export_format: str = typer.Option("csv", "--format"),
    device_ids: list[str] | None = typer.Option(None, "--device-id"),
    device_type: str = typer.Option("", "--device-type"),
    point_table_id: str = typer.Option("", "--point-table-id"),
    point_ids: list[str] | None = typer.Option(None, "--point-id"),
    qualities: list[int] | None = typer.Option(None, "--quality"),
    schema_fingerprint: str = typer.Option("", "--schema-fingerprint"),
) -> None:
    """Create an authorized grouped wide-history CSV/JSON export."""
    if export_format not in {"csv", "json"}:
        raise typer.BadParameter("export format must be csv or json")
    _invoke(
        "CreateExport",
        {
            "request_id": request_id,
            "export_format": export_format,
            "filter": _history_filter(
                wind_farm_id=wind_farm_id,
                start_time=start_time,
                end_time=end_time,
                level=level,
                device_ids=device_ids,
                device_type=device_type,
                point_table_id=point_table_id,
                point_ids=point_ids,
                qualities=qualities,
                schema_fingerprint=schema_fingerprint,
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
