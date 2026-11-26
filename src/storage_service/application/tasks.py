"""Durable asynchronous CSV export and verified external TDengine backup jobs.

No task is reported SUCCEEDED without an fsync'ed, hash-verified artifact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Protocol

from storage_service.application.config import StorageConfig
from storage_service.application.wide_export import export_wide_device_history
from storage_service.application.wide_query import WideHistoryFilter, WideHistoryQueryService
from storage_service.domain.wide_schema import WideSchema
from storage_service.domain.tasks import TaskRecord

_LOG = logging.getLogger(__name__)


class TaskStore(Protocol):
    def register_task(
        self,
        *,
        task_id: str,
        request_id: str,
        request_sha256: str,
        kind: str,
        wind_farm_id: str,
        owner_id: str,
    ) -> str: ...

    def get_task(
        self,
        task_id: str,
        *,
        kind: str,
        owner_id: str,
    ) -> TaskRecord | None: ...

    def set_task_state(
        self,
        task_id: str,
        *,
        state: str,
        artifact_id: str = "",
        size_bytes: int = 0,
        sha256: str = "",
        error_code: str = "",
        message: str = "",
    ) -> None: ...


class NativeBackupPort(Protocol):
    async def backup(
        self,
        *,
        task_id: str,
        wind_farm_id: str,
    ) -> tuple[str, int, str]:
        """Return artifact identifier, verified total bytes, and manifest SHA256."""


class ExportQuotaExceededError(RuntimeError):
    """An export would exceed the authorized output limit."""


class AsyncStorageTasks:
    """One process-owned task coordinator, with SQLite idempotency and budgets."""

    def __init__(
        self,
        *,
        config: StorageConfig,
        metadata: TaskStore,
        wide_history: WideHistoryQueryService,
        backup: NativeBackupPort,
    ) -> None:
        self._config = config
        self._metadata = metadata
        self._wide_history = wide_history
        self._backup = backup
        self._backup_semaphore = asyncio.Semaphore(config.backup.max_jobs)
        self._semaphore = asyncio.Semaphore(config.export.max_jobs)
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def active_exports(self) -> int:
        return sum(
            not job.done() and job.get_name().startswith("storage-export-") for job in self._pending
        )

    @property
    def active_backups(self) -> int:
        return sum(
            not job.done() and job.get_name().startswith("storage-backup-") for job in self._pending
        )

    @staticmethod
    def _request_digest(values: list[object]) -> str:
        payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

    async def create_wide_export(
        self,
        *,
        request_id: str,
        filter_: WideHistoryFilter,
        schema: WideSchema,
        device_type: str,
        export_format: str,
        owner_id: str,
    ) -> TaskRecord:
        """Register one immutable schema-bound CSV/JSON export job."""
        if not request_id or len(request_id) > 256:
            raise ValueError("invalid wide export request_id")
        if export_format not in {"csv", "json"}:
            raise ValueError("wide export format must be csv or json")
        filter_.validate(schema, maximum=self._config.query.max_page_size)
        if filter_.after_time_us is not None:
            raise ValueError("wide export cannot accept an external pagination cursor")
        task_id = uuid.uuid4().hex
        fingerprint = self._request_digest([
            "WIDE_EXPORT_V1",
            filter_.wind_farm_id,
            filter_.device_id,
            filter_.device_config_id,
            filter_.data_level.value,
            filter_.start_time_us,
            filter_.end_time_us,
            filter_.point_ids,
            filter_.qualities,
            schema.fingerprint,
            export_format,
        ])
        existing = await asyncio.to_thread(
            self._metadata.register_task,
            task_id=task_id,
            request_id=request_id,
            request_sha256=fingerprint,
            kind="EXPORT",
            wind_farm_id=filter_.wind_farm_id,
            owner_id=owner_id,
        )
        stored = self.get_export(existing, owner_id=owner_id)
        if stored is None:
            raise RuntimeError("registered wide export task not found")
        if existing == task_id:
            worker = asyncio.create_task(
                self._run_wide_export(
                    task_id=task_id, filter_=filter_, schema=schema,
                    device_type=device_type, export_format=export_format,
                ),
                name=f"storage-export-{task_id}",
            )
            self._pending.add(worker)
            worker.add_done_callback(self._completed)
        return stored

    async def _run_wide_export(
        self,
        *,
        task_id: str,
        filter_: WideHistoryFilter,
        schema: WideSchema,
        device_type: str,
        export_format: str,
    ) -> None:
        async with self._semaphore:
            await asyncio.to_thread(self._metadata.set_task_state, task_id, state="RUNNING")
            try:
                artifact, size, digest = await export_wide_device_history(
                    history=self._wide_history, schema=schema,
                    filter_=filter_, device_type=device_type,
                    artifact_id=task_id, export_format=export_format,
                    root=self._config.export.root,
                    max_bytes=self._config.export.max_bytes,
                )
                await asyncio.to_thread(
                    self._metadata.set_task_state,
                    task_id, state="SUCCEEDED",
                    artifact_id=artifact, size_bytes=size, sha256=digest,
                )
            except BaseException as exc:
                await asyncio.to_thread(
                    self._metadata.set_task_state, task_id,
                    state="FAILED", error_code="WIDE_EXPORT_FAILED",
                    message=type(exc).__name__,
                )
                raise

    def get_export(self, task_id: str, *, owner_id: str) -> TaskRecord | None:
        return self._metadata.get_task(task_id, kind="EXPORT", owner_id=owner_id)

    def get_backup(self, task_id: str, *, owner_id: str) -> TaskRecord | None:
        return self._metadata.get_task(task_id, kind="BACKUP", owner_id=owner_id)

    async def create_backup(
        self,
        *,
        request_id: str,
        wind_farm_id: str,
        owner_id: str,
    ) -> TaskRecord:
        if not request_id or not wind_farm_id:
            raise ValueError("backup request ID and farm must be nonempty")
        if len(request_id) > 256 or len(wind_farm_id.encode("utf-8")) > 256:
            raise ValueError("backup request identity too long")
        task_id = uuid.uuid4().hex
        fingerprint = self._request_digest(["BACKUP", wind_farm_id])
        actual = await asyncio.to_thread(
            self._metadata.register_task,
            task_id=task_id,
            request_id=request_id,
            request_sha256=fingerprint,
            kind="BACKUP",
            wind_farm_id=wind_farm_id,
            owner_id=owner_id,
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
            await asyncio.to_thread(self._metadata.set_task_state, task_id, state="RUNNING")
            try:
                artifact, size, digest = await self._backup.backup(
                    task_id=task_id,
                    wind_farm_id=wind_farm_id,
                )
                await asyncio.to_thread(
                    self._metadata.set_task_state,
                    task_id,
                    state="SUCCEEDED",
                    artifact_id=artifact,
                    size_bytes=size,
                    sha256=digest,
                )
            except BaseException as exc:
                await asyncio.to_thread(
                    self._metadata.set_task_state,
                    task_id,
                    state="FAILED",
                    error_code="NATIVE_BACKUP_FAILED",
                    message=type(exc).__name__,
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
