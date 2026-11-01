"""Immutable Commander process settings model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    file_version: int
    queue_timeout: float
    connect_timeout: float
    write_timeout: float
    idle_timeout: float
    watchdog_interval: float
    global_capacity: int
    max_queued_batches_per_device: int
    max_queued_batches_global: int
    max_batch_points: int
    sqlite_path: Path
    max_storage_bytes: int
    reserved_storage_bytes: int
    admission_stop_ratio: float
    retention_seconds: int
    cleanup_interval_seconds: float
    audit_stream: str
    audit_subject: str
    nats_servers: tuple[str, ...]
    audit_retry_base: float
    audit_retry_max: float
    shutdown_grace_seconds: float
