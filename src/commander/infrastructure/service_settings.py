"""Strict Commander-owned service settings; never hot-applied."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.infrastructure.config.document_version import validate_document_version
from core.infrastructure.config.yaml import read_yaml_mapping


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    file_version: str
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
    audit_stream: str
    audit_subject: str


def _mapping(raw: object, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise ValueError(f"{name}: expected string-keyed mapping")
    return raw


def _keys(raw: dict[str, Any], allowed: set[str], name: str) -> None:
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f"{name}: unexpected fields {sorted(extra)}")


def _positive(raw: object, name: str, *, integer: bool = False) -> float | int:
    if integer:
        if type(raw) is not int or raw < 1:
            raise ValueError(f"{name}: expected positive integer")
        return raw
    if type(raw) not in (int, float) or not 0 < raw < float("inf"):
        raise ValueError(f"{name}: expected finite positive number")
    return float(raw)


def load_service_settings(path: str | Path) -> ServiceSettings:
    """Read service-owned settings once. Caller compares files on reload."""
    config_path = Path(path)
    raw = read_yaml_mapping(config_path)
    validate_document_version(raw, config_path)
    _keys(
        raw,
        {
            "schema_version", "file_version", "dispatch", "timeouts",
            "storage", "audit", "runtime",
        },
        "commander service",
    )
    dispatch = _mapping(raw.get("dispatch", {}), "dispatch")
    timeouts = _mapping(raw.get("timeouts", {}), "timeouts")
    storage = _mapping(raw.get("storage"), "storage")
    audit = _mapping(raw.get("audit"), "audit")
    runtime = _mapping(raw.get("runtime", {}), "runtime")
    _keys(dispatch, {
        "global_capacity", "max_queued_batches_per_device",
        "max_queued_batches_global", "max_batch_points",
    }, "dispatch")
    _keys(timeouts, {
        "queue", "connect", "write", "idle",
    }, "timeouts")
    _keys(storage, {"sqlite_path"}, "storage")
    _keys(audit, {"stream", "subject"}, "audit")
    _keys(runtime, {"watchdog_interval"}, "runtime")

    sqlite_path = storage.get("sqlite_path")
    stream = audit.get("stream")
    subject = audit.get("subject")
    if not all(isinstance(v, str) and v.strip() for v in (sqlite_path, stream, subject)):
        raise ValueError("storage.sqlite_path and audit stream/subject required")
    return ServiceSettings(
        file_version=str(raw["file_version"]),
        queue_timeout=float(_positive(timeouts.get("queue", 5), "timeouts.queue")),
        connect_timeout=float(_positive(timeouts.get("connect", 3), "timeouts.connect")),
        write_timeout=float(_positive(timeouts.get("write", 3), "timeouts.write")),
        idle_timeout=float(_positive(timeouts.get("idle", 30), "timeouts.idle")),
        watchdog_interval=float(
            _positive(runtime.get("watchdog_interval", 5), "runtime.watchdog_interval")
        ),
        global_capacity=int(
            _positive(dispatch.get("global_capacity", 32), "dispatch.global_capacity", integer=True)
        ),
        max_queued_batches_per_device=int(
            _positive(dispatch.get("max_queued_batches_per_device", 10), "dispatch.device_batches", integer=True)
        ),
        max_queued_batches_global=int(
            _positive(dispatch.get("max_queued_batches_global", 1000), "dispatch.global_batches", integer=True)
        ),
        max_batch_points=int(
            _positive(dispatch.get("max_batch_points", 10), "dispatch.batch_points", integer=True)
        ),
        sqlite_path=Path(sqlite_path),
        audit_stream=stream,
        audit_subject=subject,
    )
