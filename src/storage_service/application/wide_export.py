"""Bounded version-pinned CSV/JSON device-wide history export."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TextIO

from storage_service.application.wide_query import (
    WideHistoryFilter,
    WideHistoryQueryService,
)
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_schema import WideSchema


class WideExportQuotaExceededError(RuntimeError):
    """Export artifact reached the configured physical byte limit."""


def _safe_csv(value: object) -> object:
    if isinstance(value, bytes | bytearray | memoryview):
        value = base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _json_scalar(value: object) -> object:
    if isinstance(value, bytes | bytearray | memoryview):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError("invalid TDengine wide export scalar")


def _check_quota(file: TextIO, *, max_bytes: int) -> None:
    """Limit buffered export growth before another source row is accepted."""
    file.flush()
    if os.fstat(file.fileno()).st_size > max_bytes:
        raise WideExportQuotaExceededError(
            "wide export exceeded configured byte quota"
        )


def _synchronize(file: TextIO) -> int:
    file.flush()
    os.fsync(file.fileno())
    return os.fstat(file.fileno()).st_size


async def export_wide_device_history(
    *,
    history: WideHistoryQueryService,
    schema: WideSchema,
    filter_: WideHistoryFilter,
    device_type: str,
    artifact_id: str,
    export_format: str,
    root: Path,
    max_bytes: int,
) -> tuple[str, int, str]:
    """Write an immutable physical schema family to one atomic bounded artifact."""
    if export_format not in {"csv", "json"}:
        raise ValueError("wide export supports csv and json")
    if max_bytes <= 0:
        raise ValueError("wide export quota must be positive")
    if (
        not artifact_id.isascii() or not artifact_id.isalnum()
        or len(artifact_id) > 128
    ):
        raise ValueError("wide export artifact identity invalid")
    filter_.validate(schema, maximum=1000)
    if filter_.after_time_us is not None:
        raise ValueError("wide export requires signed pagination only")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("wide export directory cannot be a symbolic link")
    output = root / f"{artifact_id}.{export_format}"
    if output.exists() or output.is_symlink():
        raise FileExistsError("wide export artifact already exists")
    fields = {column.point_id: column for column in schema.columns}
    selected = tuple(fields[point_id] for point_id in filter_.point_ids)
    temporary: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=root,
            prefix=".storage-wide-export-", delete=False,
        ) as file:
            os.fchmod(file.fileno(), 0o600)
            temporary = Path(file.name)
            csv_writer = csv.writer(file) if export_format == "csv" else None
            if csv_writer is not None:
                csv_writer.writerow(
                    ["sample_time_us", *(
                        name for column in selected
                        for name in (column.column_name, f"{column.column_name}__quality")
                    )]
                )
            else:
                header = {
                    "wind_farm_id": filter_.wind_farm_id,
                    "device_id": filter_.device_id,
                    "device_config_id": filter_.device_config_id,
                    "data_level": filter_.data_level.value,
                    "point_table_id": schema.point_table_id,
                    "point_table_version": schema.point_table_version,
                    "schema_fingerprint": schema.fingerprint,
                    "columns": [
                        {
                            "point_id": col.point_id,
                            "name": col.column_name,
                            "sql_type": col.sql_type,
                        } for col in selected
                    ],
                }
                file.write(json.dumps(header, ensure_ascii=False)[:-1] + ',"rows":[')
            token = ""
            seen: set[str] = set()
            row_count = 0
            previous_time_us: int | None = None
            while True:
                page = await history.query(
                    schema=schema, filter_=filter_, device_type=device_type,
                    page_token=token,
                )
                if page.schema_fingerprint != schema.fingerprint:
                    raise RuntimeError("wide export page schema fingerprint mismatch")
                if not page.rows and not page.complete:
                    raise RuntimeError("wide export pagination returned empty intermediate page")
                for row in page.rows:
                    if (
                        type(row.sample_time_us) is not int
                        or not filter_.start_time_us <= row.sample_time_us < filter_.end_time_us
                        or (
                            previous_time_us is not None
                            and row.sample_time_us <= previous_time_us
                        )
                    ):
                        raise RuntimeError("wide export page timestamp order violated")
                    previous_time_us = row.sample_time_us
                    if len(row.points) != len(selected):
                        raise RuntimeError("wide export returned incomplete row")
                    for column, (point_id, value, quality) in zip(
                        selected, row.points, strict=True
                    ):
                        if column.point_id != point_id or type(quality) not in (int, PointQuality):
                            raise RuntimeError("wide export returned incompatible physical column")
                        if not 0 <= int(quality) <= 3:
                            raise RuntimeError("wide export returned invalid quality")
                        if (
                            (int(quality) == 0 and value is not None)
                            or (int(quality) == 1 and value is None)
                        ):
                            raise RuntimeError("wide export NULL/quality mismatch")
                    if csv_writer is not None:
                        cells: list[object] = [row.sample_time_us]
                        for column, item in zip(selected, row.points, strict=True):
                            if column.point_id != item[0]:
                                raise RuntimeError("wide export column mismatch")
                            cells.extend((_safe_csv(item[1]), int(item[2])))
                        csv_writer.writerow(cells)
                    else:
                        record = {
                            "sample_time_us": row.sample_time_us,
                            "points": {
                                point_id: [_json_scalar(value), int(quality)]
                                for point_id, value, quality in row.points
                            },
                        }
                        if row_count:
                            file.write(",")
                        file.write(json.dumps(record, ensure_ascii=False, allow_nan=False))
                    row_count += 1
                    _check_quota(file, max_bytes=max_bytes)
                size = await asyncio.to_thread(_synchronize, file)
                if size > max_bytes:
                    raise WideExportQuotaExceededError("wide export exceeded configured byte quota")
                if page.complete:
                    break
                if not page.next_page_token or page.next_page_token in seen:
                    raise RuntimeError("wide export pagination failed to progress")
                if len(seen) >= 10_000:
                    raise RuntimeError("wide export pagination exceeded safety limit")
                seen.add(page.next_page_token)
                token = page.next_page_token
            if csv_writer is None:
                file.write("]}")
            size = await asyncio.to_thread(_synchronize, file)
            if size > max_bytes:
                raise WideExportQuotaExceededError("wide export exceeded configured byte quota")
        if temporary is None:
            raise RuntimeError("wide export temporary artifact was not created")
        if not history.read_available():
            raise RuntimeError("wide export blocked by Kafka replay before publication")
        with temporary.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if not history.read_available():
            raise RuntimeError("wide export replay changed during artifact hashing")
        os.link(temporary, output)
        published = True
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return output.name, size, digest
    except BaseException:
        if published:
            output.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
