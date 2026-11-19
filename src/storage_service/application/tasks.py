"""Durable asynchronous CSV export and verified external TDengine backup jobs.

No task is reported SUCCEEDED without an fsync'ed, hash-verified artifact.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from storage_service.application.config import StorageConfig
from storage_service.application.query import HistoryFilter, HistoryQueryService
from storage_service.domain.tasks import TaskRecord


_LOG = logging.getLogger(__name__)


class TaskStore(Protocol):
    def register_task(
        self, *, task_id: str, request_id: str, request_sha256: str,
        kind: str, wind_farm_id: str, owner_id: str,
    ) -> str: ...

    def get_task(
        self, task_id: str, *, kind: str, owner_id: str,
    ) -> TaskRecord | None: ...

    def set_task_state(
        self, task_id: str, *, state: str, artifact_id: str = "",
        size_bytes: int = 0, sha256: str = "", error_code: str = "",
        message: str = "",
    ) -> None: ...


class NativeBackupPort(Protocol):
    async def backup(
        self, *, task_id: str, wind_farm_id: str,
    ) -> tuple[str, int, str]:
        """Return artifact identifier, verified total bytes, and manifest SHA256."""


class ExportQuotaExceeded(RuntimeError):
    """An export would exceed the authorized output limit."""


class AsyncStorageTasks:
    """One process-owned task coordinator, with SQLite idempotency and budgets."""

    def __init__(
        self, *, config: StorageConfig, metadata: TaskStore,
        history: HistoryQueryService, backup: NativeBackupPort,
    ) -> None:
        self._config = config
        self._metadata = metadata
        self._history = history
        self._backup = backup
        self._backup_semaphore = asyncio.Semaphore(config.backup.max_jobs)
        self._semaphore = asyncio.Semaphore(config.export.max_jobs)
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def active_exports(self) -> int:
        return sum(
            not job.done() and job.get_name().startswith("storage-export-")
            for job in self._pending
        )

    @property
    def active_backups(self) -> int:
        return sum(
            not job.done() and job.get_name().startswith("storage-backup-")
            for job in self._pending
        )

    @staticmethod
    def _request_digest(values: list[object]) -> str:
        payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

    def get_export(self, task_id: str, *, owner_id: str) -> TaskRecord | None:
        return self._metadata.get_task(task_id, kind="EXPORT", owner_id=owner_id)

    async def create_export(
        self, *, request_id: str, filter_: HistoryFilter,
        export_format: str, owner_id: str,
    ) -> TaskRecord:
        if not request_id or len(request_id) > 256:
            raise ValueError("invalid export request_id")
        if export_format != "csv":
            raise ValueError("only CSV export is supported")
        filter_.validate(max_range_days=self._config.query.max_range_days)
        task_id = uuid.uuid4().hex
        fingerprint = self._request_digest(["EXPORT", filter_.fingerprint(), export_format])
        existing = await asyncio.to_thread(
            self._metadata.register_task,
            task_id=task_id, request_id=request_id,
            request_sha256=fingerprint, kind="EXPORT",
            wind_farm_id=filter_.wind_farm_id, owner_id=owner_id,
        )
        record = self.get_export(existing, owner_id=owner_id)
        if record is None:
            raise RuntimeError("durable export task was not retrievable")
        if existing == task_id:
            worker = asyncio.create_task(
                self._run_export(task_id, filter_), name=f"storage-export-{task_id}"
            )
            self._pending.add(worker)
            worker.add_done_callback(self._completed)
        return record

    async def _run_export(self, task_id: str, filter_: HistoryFilter) -> None:
        async with self._semaphore:
            await asyncio.to_thread(
                self._metadata.set_task_state, task_id, state="RUNNING"
            )
            try:
                artifact = await self._export_file(task_id, filter_)
                await asyncio.to_thread(
                    self._metadata.set_task_state,
                    task_id, state="SUCCEEDED",
                    artifact_id=artifact[0], size_bytes=artifact[1], sha256=artifact[2],
                )
            except BaseException as exc:
                await asyncio.to_thread(
                    self._metadata.set_task_state,
                    task_id, state="FAILED", error_code="EXPORT_FAILED",
                    message=type(exc).__name__,
                )
                raise

    async def _export_file(
        self, task_id: str, filter_: HistoryFilter,
    ) -> tuple[str, int, str]:
        root = self._config.export.root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError("export root must not be a symbolic link")
        output = root / f"{task_id}.csv"
        if output.exists():
            raise RuntimeError("export artifact ID already exists")
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=root,
                prefix=".storage-export-", delete=False,
            ) as file:
                os.fchmod(file.fileno(), 0o600)
                tmp_path = Path(file.name)
                writer = csv.writer(file)
                writer.writerow([
                    "wind_farm_id", "device_type", "device_id",
                    "point_table_id", "data_level", "sample_time_us",
                    "sample_id", "point_id", "business_point_id",
                    "value_kind", "value", "quality",
                    "logical_key", "content_sha256",
                ])
                token = ""
                seen: set[str] = set()
                while True:
                    page = await self._history.query(
                        filter_, page_size=200, page_token=token
                    )
                    for row in page.rows:
                        value = row.value
                        if isinstance(value, bytes):
                            value = base64.b64encode(value).decode("ascii")
                        cells: list[object] = [
                            *row.identity[:5], row.ts_us, row.sample_id,
                            row.point_id, row.business_point_id,
                            row.value_kind, value, row.quality,
                            row.logical_key, row.content_sha256,
                        ]
                        # Neutralize spreadsheet formulas in user-supplied identifiers.
                        safe = [
                            "'" + cell if isinstance(cell, str) and cell.lstrip().startswith(
                                ("=", "+", "-", "@")
                            ) else cell
                            for cell in cells
                        ]
                        writer.writerow(safe)
                    file.flush()
                    if os.fstat(file.fileno()).st_size > self._config.export.max_bytes:
                        raise ExportQuotaExceeded("CSV export exceeded configured quota")
                    if page.complete:
                        break
                    if not page.next_page_token or page.next_page_token in seen:
                        raise RuntimeError("history cursor stopped advancing")
                    seen.add(page.next_page_token)
                    token = page.next_page_token
                file.flush()
                os.fsync(file.fileno())
            # File must not become visible before the final digest is known.
            if tmp_path is None:
                raise RuntimeError("temporary export path missing")
            size = tmp_path.stat().st_size
            with tmp_path.open("rb") as artifact:
                digest = hashlib.file_digest(artifact, "sha256").hexdigest()
            os.link(tmp_path, output)
            dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return output.name, size, digest
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)


    def get_backup(self, task_id: str, *, owner_id: str) -> TaskRecord | None:
        return self._metadata.get_task(task_id, kind="BACKUP", owner_id=owner_id)

    async def create_backup(
        self, *, request_id: str, wind_farm_id: str, owner_id: str,
    ) -> TaskRecord:
        if not request_id or not wind_farm_id:
            raise ValueError("backup request ID and farm must be nonempty")
        if len(request_id) > 256 or len(wind_farm_id.encode("utf-8")) > 256:
            raise ValueError("backup request identity too long")
        task_id = uuid.uuid4().hex
        fingerprint = self._request_digest(["BACKUP", wind_farm_id])
        actual = await asyncio.to_thread(
            self._metadata.register_task,
            task_id=task_id, request_id=request_id,
            request_sha256=fingerprint, kind="BACKUP",
            wind_farm_id=wind_farm_id, owner_id=owner_id,
        )
        record = self.get_backup(actual, owner_id=owner_id)
        if record is None:
            raise RuntimeError("durable backup task was not retrievable")
        if actual == task_id:
            job = asyncio.create_task(
                self._run_backup(task_id, wind_farm_id),
                name=f"storage-backup-{task_id}",
            )
            self._pending.add(job)
            job.add_done_callback(self._completed)
        return record

    async def _run_backup(self, task_id: str, wind_farm_id: str) -> None:
        async with self._backup_semaphore:
            await asyncio.to_thread(
                self._metadata.set_task_state, task_id, state="RUNNING"
            )
            try:
                artifact, size, digest = await self._backup.backup(
                    task_id=task_id, wind_farm_id=wind_farm_id,
                )
                await asyncio.to_thread(
                    self._metadata.set_task_state, task_id, state="SUCCEEDED",
                    artifact_id=artifact, size_bytes=size, sha256=digest,
                )
            except BaseException as exc:
                await asyncio.to_thread(
                    self._metadata.set_task_state, task_id, state="FAILED",
                    error_code="NATIVE_BACKUP_FAILED", message=type(exc).__name__,
                )
                raise

    def _completed(self, future: asyncio.Task[None]) -> None:
        self._pending.discard(future)
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            _LOG.error("Storage background task failed: %s", type(error).__name__)

    async def close(self) -> None:
        for future in tuple(self._pending):
            future.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
