"""Strict Commander-owned service settings; never hot-applied."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from commander.application.settings import ServiceSettings
from core.infrastructure.config.yaml import read_yaml_mapping


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
    if type(raw) not in (int, float) or not isinstance(raw, int | float):
        raise ValueError(f"{name}: expected finite positive number")
    if not 0 < raw < float("inf"):
        raise ValueError(f"{name}: expected finite positive number")
    return float(raw)


def load_service_settings(path: str | Path) -> ServiceSettings:
    """Validate the v1_1 Commander service configuration; never hot-apply it."""
    raw = read_yaml_mapping(Path(path))
    _keys(
        raw,
        {
            "schema_version",
            "file_version",
            "grpc",
            "security",
            "dispatcher",
            "repository",
            "audit",
            "timeouts",
            "shutdown",
        },
        "commander service",
    )
    if raw.get("schema_version") != 1 or type(raw.get("file_version")) is not int:
        raise ValueError("unsupported Commander service schema/file version")
    dispatch = _mapping(raw.get("dispatcher", {}), "dispatcher")
    timeouts = _mapping(raw.get("timeouts", {}), "timeouts")
    repository = _mapping(raw.get("repository"), "repository")
    audit = _mapping(raw.get("audit"), "audit")
    _keys(
        dispatch,
        {
            "max_concurrency",
            "max_pending_per_device",
            "max_pending_global",
            "max_batch_size",
        },
        "dispatcher",
    )
    _keys(
        timeouts,
        {"queue_default_ms", "write_default_ms", "connect_ms"},
        "timeouts",
    )
    _keys(
        repository,
        {
            "sqlite_path",
            "max_storage_bytes",
            "reserved_storage_bytes",
            "retention_seconds",
            "cleanup_interval_seconds",
            "storage_warning_ratio",
            "admission_stop_ratio",
        },
        "repository",
    )
    _keys(
        audit,
        {"nats_servers", "stream", "subject", "retry_base_ms", "retry_max_ms"},
        "audit",
    )
    shutdown = _mapping(raw.get("shutdown", {}), "shutdown")
    _keys(shutdown, {"grace_seconds"}, "shutdown")
    sqlite_path = repository.get("sqlite_path")
    stream = audit.get("stream")
    subject = audit.get("subject")
    for value, label in (
        (sqlite_path, "repository.sqlite_path"),
        (stream, "audit.stream"),
        (subject, "audit.subject"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} required")
    assert isinstance(sqlite_path, str)
    assert isinstance(stream, str)
    assert isinstance(subject, str)
    max_storage = int(
        _positive(
            repository.get("max_storage_bytes", 64 * 1024**3),
            "max_storage_bytes",
            integer=True,
        )
    )
    reserved = int(
        _positive(
            repository.get("reserved_storage_bytes", 6871947674),
            "reserved_storage_bytes",
            integer=True,
        )
    )
    stop_ratio = repository.get("admission_stop_ratio", 0.9)
    warning_ratio = repository.get("storage_warning_ratio", 0.8)
    if not (
        type(stop_ratio) in (float, int)
        and type(warning_ratio) in (float, int)
        and 0 < warning_ratio < stop_ratio < 1
        and reserved < max_storage
    ):
        raise ValueError("invalid storage capacity and admission ratios")
    servers = audit.get("nats_servers")
    if (
        not isinstance(servers, list)
        or not servers
        or any(not isinstance(server, str) or not server.strip() for server in servers)
    ):
        raise ValueError("audit.nats_servers requires a non-empty list")
    retry_base = float(_positive(audit.get("retry_base_ms", 100), "retry_base_ms")) / 1000
    retry_max = float(_positive(audit.get("retry_max_ms", 5000), "retry_max_ms")) / 1000
    if retry_max < retry_base:
        raise ValueError("audit retry_max_ms must be >= retry_base_ms")
    for section in ("grpc", "security"):
        if section in raw:
            _mapping(raw[section], section)
    return ServiceSettings(
        file_version=raw["file_version"],
        queue_timeout=(
            float(_positive(timeouts.get("queue_default_ms", 1000), "queue_default_ms")) / 1000
        ),
        connect_timeout=(float(_positive(timeouts.get("connect_ms", 3000), "connect_ms")) / 1000),
        write_timeout=(
            float(_positive(timeouts.get("write_default_ms", 500), "write_default_ms")) / 1000
        ),
        idle_timeout=30.0,
        watchdog_interval=5.0,
        global_capacity=int(
            _positive(
                dispatch.get("max_concurrency", 100),
                "max_concurrency",
                integer=True,
            )
        ),
        max_queued_batches_per_device=int(
            _positive(
                dispatch.get("max_pending_per_device", 10),
                "max_pending_per_device",
                integer=True,
            )
        ),
        max_queued_batches_global=int(
            _positive(dispatch.get("max_pending_global", 1000), "max_pending_global", integer=True)
        ),
        max_batch_points=int(
            _positive(dispatch.get("max_batch_size", 10), "max_batch_size", integer=True)
        ),
        sqlite_path=Path(sqlite_path),
        max_storage_bytes=max_storage,
        reserved_storage_bytes=reserved,
        admission_stop_ratio=float(stop_ratio),
        retention_seconds=int(
            _positive(repository.get("retention_seconds", 1800), "retention_seconds", integer=True)
        ),
        cleanup_interval_seconds=float(
            _positive(repository.get("cleanup_interval_seconds", 60), "cleanup_interval_seconds")
        ),
        audit_stream=stream,
        audit_subject=subject,
        nats_servers=tuple(servers),
        audit_retry_base=retry_base,
        audit_retry_max=retry_max,
        shutdown_grace_seconds=float(
            _positive(shutdown.get("grace_seconds", 10), "shutdown.grace_seconds")
        ),
    )
