"""Atomic bounded exports across immutable wide-table schema groups."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import IO

from storage_service.application.wide_group_query import (
    GroupedWideSelector,
    WideGroupedHistoryService,
)
from storage_service.domain.quality_bitmap import PointQuality


class WideExportQuotaExceededError(RuntimeError):
    """An export exceeded its configured physical byte limit."""


def _csv_value(value: object) -> object:
    if isinstance(value, bytes | bytearray | memoryview):
        value = base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _json_value(value: object) -> object:
    if isinstance(value, bytes | bytearray | memoryview):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("invalid physical wide export value")


def _synchronize(file: IO[str]) -> int:
    file.flush()
    os.fsync(file.fileno())
    return os.fstat(file.fileno()).st_size


async def export_grouped_wide_history(
    *,
    history: WideGroupedHistoryService,
    selector: GroupedWideSelector,
    artifact_id: str,
    export_format: str,
    root: Path,
    max_bytes: int,
) -> tuple[str, int, str]:
    """Publish JSON groups or a ZIP of version-separated wide CSV files."""
    selector.validate()
    if export_format not in {"csv", "json"}:
        raise ValueError("grouped wide export supports csv and json")
    if (
        not artifact_id.isascii()
        or not artifact_id.isalnum()
        or len(artifact_id) > 128
        or max_bytes <= 0
    ):
        raise ValueError("invalid grouped wide export limits or identity")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("wide export root cannot be a symbolic link")
    suffix = "zip" if export_format == "csv" else "json"
    published_name = f"{artifact_id}.{suffix}"
    target = root / published_name
    if target.exists() or target.is_symlink():
        raise FileExistsError("grouped export artifact already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root, prefix=".storage-group-export-", suffix=f".{suffix}"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    published = False
    try:
        with tempfile.TemporaryDirectory(dir=root, prefix=".storage-export-groups-") as staging:
            directory = Path(staging)
            json_file: IO[str] | None = None
            csv_file: IO[str] | None = None
            csv_path: Path | None = None
            manifest: list[dict[str, object]] = []
            total_csv_bytes = 0
            current: tuple[str, str, str, str] | None = None
            last_sample: int | None = None
            group_row_count = 0
            token = ""
            seen: set[str] = set()
            if export_format == "json":
                json_file = temporary.open("w", encoding="utf-8", newline="")
                json_file.write('{"groups":[')
            try:
                while True:
                    page = await history.query(
                        selector=selector, page_size=100, page_token=token
                    )
                    for group in page.groups:
                        identity = group.identity
                        schema = group.schema
                        key = (
                            identity.wind_farm_id,
                            identity.device_id,
                            identity.device_config_id,
                            schema.fingerprint,
                        )
                        fields = {column.point_id: column for column in schema.columns}
                        selected = tuple(fields[point] for point in group.point_ids)
                        if key != current:
                            if csv_file is not None:
                                total_csv_bytes += _synchronize(csv_file)
                                csv_file.close()
                                csv_file = None
                            elif json_file is not None and current is not None:
                                json_file.write("]}")
                            if json_file is not None:
                                if current is not None:
                                    json_file.write(",")
                                header = {
                                    "wind_farm_id": identity.wind_farm_id,
                                    "device_id": identity.device_id,
                                    "device_config_id": identity.device_config_id,
                                    "point_table_id": schema.point_table_id,
                                    "point_table_version": schema.point_table_version,
                                    "data_level": identity.data_level.value,
                                    "schema_fingerprint": schema.fingerprint,
                                    "columns": [
                                        {
                                            "point_id": column.point_id,
                                            "physical_name": column.column_name,
                                            "sql_type": column.sql_type,
                                        }
                                        for column in selected
                                    ],
                                }
                                json_file.write(
                                    json.dumps(header, ensure_ascii=False)[:-1] + ',"rows":['
                                )
                            else:
                                csv_path = directory / f"group_{len(manifest):04d}.csv"
                                csv_file = csv_path.open("w", encoding="utf-8", newline="")
                                csv.writer(csv_file).writerow(
                                    [
                                        "wind_farm_id",
                                        "device_id",
                                        "device_config_id",
                                        "point_table_id",
                                        "point_table_version",
                                        "data_level",
                                        "schema_fingerprint",
                                        "sample_time_us",
                                        *(
                                            name
                                            for column in selected
                                            for name in (
                                                column.column_name,
                                                f"{column.column_name}__quality",
                                            )
                                        ),
                                    ]
                                )
                                manifest.append(
                                    {
                                        "file": csv_path.name,
                                        "wind_farm_id": identity.wind_farm_id,
                                        "device_id": identity.device_id,
                                        "device_config_id": identity.device_config_id,
                                        "point_table_id": schema.point_table_id,
                                        "data_level": identity.data_level.value,
                                        "schema_fingerprint": schema.fingerprint,
                                    }
                                )
                            current = key
                            last_sample = None
                            group_row_count = 0
                        for row in group.rows:
                            if (
                                type(row.sample_time_us) is not int
                                or not selector.start_time_us <= row.sample_time_us
                                < selector.end_time_us
                                or (
                                    last_sample is not None
                                    and row.sample_time_us <= last_sample
                                )
                            ):
                                raise RuntimeError("grouped export has invalid timestamp order")
                            last_sample = row.sample_time_us
                            if len(row.points) != len(selected):
                                raise RuntimeError("grouped export returned incomplete columns")
                            values: list[object] = []
                            for column, (point, value, quality) in zip(
                                selected, row.points, strict=True
                            ):
                                if (
                                    column.point_id != point
                                    or type(quality) not in (int, PointQuality)
                                    or not 0 <= int(quality) <= 3
                                    or (int(quality) == 0 and value is not None)
                                ):
                                    raise RuntimeError("grouped export invalid physical value")
                                values.extend((_csv_value(value), int(quality)))
                            if json_file is not None:
                                if group_row_count:
                                    json_file.write(",")
                                json_file.write(
                                    json.dumps(
                                        {
                                            "sample_time_us": row.sample_time_us,
                                            "points": {
                                                point: [_json_value(value), int(quality)]
                                                for point, value, quality in row.points
                                            },
                                        },
                                        ensure_ascii=False,
                                        allow_nan=False,
                                    )
                                )
                                json_file.flush()
                                if os.fstat(json_file.fileno()).st_size > max_bytes:
                                    raise WideExportQuotaExceededError(
                                        "grouped JSON quota exceeded"
                                    )
                            else:
                                if csv_file is None:
                                    raise RuntimeError("grouped CSV stream unavailable")
                                csv.writer(csv_file).writerow(
                                    [
                                        identity.wind_farm_id,
                                        identity.device_id,
                                        identity.device_config_id,
                                        schema.point_table_id,
                                        schema.point_table_version,
                                        identity.data_level.value,
                                        schema.fingerprint,
                                        row.sample_time_us,
                                        *values,
                                    ]
                                )
                                csv_file.flush()
                                if (
                                    total_csv_bytes + os.fstat(csv_file.fileno()).st_size
                                    > max_bytes
                                ):
                                    raise WideExportQuotaExceededError("grouped CSV quota exceeded")
                            group_row_count += 1
                    if page.complete:
                        break
                    if not page.next_page_token or page.next_page_token in seen:
                        raise RuntimeError("grouped export cursor failed to progress")
                    if len(seen) >= 10000:
                        raise RuntimeError("grouped export exceeds pagination safety limit")
                    seen.add(page.next_page_token)
                    token = page.next_page_token
                if csv_file is not None:
                    total_csv_bytes += _synchronize(csv_file)
                    csv_file.close()
                    csv_file = None
                if json_file is not None:
                    if current is not None:
                        json_file.write("]}")
                    json_file.write("]}")
                    _synchronize(json_file)
                    json_file.close()
                    json_file = None
                else:
                    with zipfile.ZipFile(
                        temporary, mode="w", compression=zipfile.ZIP_STORED
                    ) as archive:
                        archive.writestr(
                            "manifest.json",
                            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                        )
                        for entry in manifest:
                            filename = str(entry["file"])
                            archive.write(directory / filename, arcname=filename)
            finally:
                if json_file is not None:
                    json_file.close()
                if csv_file is not None:
                    csv_file.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
            size = os.fstat(stream.fileno()).st_size
            if size > max_bytes:
                raise WideExportQuotaExceededError("grouped export exceeded output quota")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not history.read_available():
            raise RuntimeError("Kafka replay began during grouped export")
        os.link(temporary, target)
        published = True
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return published_name, size, digest
    except BaseException:
        if published:
            target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
