"""Strict Commander-owned service settings; never hot-applied."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from commander.application.settings import ServiceSettings
from core.infrastructure.config import read_yaml


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


def validate_transport_settings(raw: dict[str, Any]) -> None:
    """Validate the service transport contract without opening network resources."""
    if "grpc" not in raw and "security" not in raw:
        return
    grpc = _mapping(raw.get("grpc"), "grpc")
    security = _mapping(raw.get("security"), "security")
    _keys(grpc, {"listen_host", "listen_port", "max_active_rpcs"}, "grpc")
    _keys(
        security,
        {"ca_cert", "server_cert", "server_key", "callers"},
        "security",
    )
    host = grpc.get("listen_host")
    port = grpc.get("listen_port")
    max_rpcs = grpc.get("max_active_rpcs")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("grpc.listen_host is required")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("grpc.listen_port must be in 1..65535")
    if type(max_rpcs) is not int or max_rpcs < 1:
        raise ValueError("grpc.max_active_rpcs must be positive")
    for name in ("ca_cert", "server_cert", "server_key"):
        value = security.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"security.{name} pathname is required")
    callers = security.get("callers")
    if not isinstance(callers, list) or not callers:
        raise ValueError("security.callers must be a nonempty list")
    seen_ids: set[str] = set()
    seen_certificates: set[str] = set()
    for index, candidate in enumerate(callers):
        caller = _mapping(candidate, f"security.callers[{index}]")
        _keys(caller, {"caller_id", "certificate_identity", "permissions"}, "caller")
        identity = caller.get("certificate_identity")
        caller_id = caller.get("caller_id")
        permissions = caller.get("permissions")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("caller certificate_identity is required")
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise ValueError("caller_id is required")
        if identity in seen_certificates or caller_id in seen_ids:
            raise ValueError("duplicate caller identity or caller_id")
        if (
            not isinstance(permissions, list)
            or not permissions
            or any(
                not isinstance(scope, str) or scope not in {"query", "control", "reload"}
                for scope in permissions
            )
            or len(permissions) != len(set(permissions))
        ):
            raise ValueError("invalid caller permissions")
        seen_certificates.add(identity)
        seen_ids.add(caller_id)


def load_service_settings(path: str | Path) -> ServiceSettings:
    """Validate the v1_1 Commander service configuration; never hot-apply it."""
    raw = read_yaml(Path(path))
    _keys(
        raw,
        {
            "schema_version",
            "file_version",
            "config_sources",
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
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported Commander service schema_version")
    file_version = raw.get("file_version")
    if file_version is not None and (
        not isinstance(file_version, str)
        or re.fullmatch(r"(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)", file_version)
        is None
    ):
        raise ValueError("commander file_version must be MAJOR.MINOR.PATCH")
    sources = _mapping(raw.get("config_sources"), "config_sources")
    _keys(sources, {"instances", "templates"}, "config_sources")
    instances = _mapping(sources.get("instances"), "config_sources.instances")
    templates = _mapping(sources.get("templates"), "config_sources.templates")
    _keys(instances, {"devices"}, "config_sources.instances")
    _keys(templates, {"root"}, "config_sources.templates")

    def source_path(value: object, name: str, *, directory: bool) -> Path:
        if not isinstance(value, str) or not value.strip() or value.strip() != value:
            raise ValueError(f"{name}: nonempty pathname required")
        candidate = Path(value)
        if ".." in candidate.parts or "\\" in value:
            raise ValueError(f"{name}: path traversal is not allowed")
        if not candidate.is_absolute():
            service_dir = Path(path).resolve().parent
            candidate = (
                service_dir.parent.joinpath(*candidate.parts[1:])
                if candidate.parts and candidate.parts[0] == "configs"
                else service_dir / candidate
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{name}: source unavailable: {candidate}") from exc
        if not (resolved.is_dir() if directory else resolved.is_file()):
            raise ValueError(f"{name}: incorrect source kind: {resolved}")
        return resolved

    devices_path = source_path(
        instances.get("devices"), "config_sources.instances.devices", directory=False
    )
    templates_root = source_path(
        templates.get("root"), "config_sources.templates.root", directory=True
    )
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
            "max_request_id_bytes",
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
            "persistence_interval_ms",
            "persistence_batch_size",
            "persistence_max_events",
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
    sqlite_file = Path(sqlite_path)
    if not sqlite_file.is_absolute():
        sqlite_file = Path(path).resolve().parent / sqlite_file
    sqlite_file = sqlite_file.resolve()
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
    validate_transport_settings(raw)
    return ServiceSettings(
        file_version=file_version,
        devices_path=devices_path,
        coordination_root=Path(path).resolve().parent.parent,
        templates_root=templates_root,
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
        max_request_id_bytes=int(
            _positive(
                dispatch.get("max_request_id_bytes", 128),
                "dispatcher.max_request_id_bytes",
                integer=True,
            )
        ),
        sqlite_path=sqlite_file,
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
        persistence_interval_seconds=(
            float(
                _positive(
                    repository.get("persistence_interval_ms", 50),
                    "repository.persistence_interval_ms",
                )
            )
            / 1000
        ),
        persistence_batch_size=int(
            _positive(
                repository.get("persistence_batch_size", 500),
                "repository.persistence_batch_size",
                integer=True,
            )
        ),
        persistence_max_events=int(
            _positive(
                repository.get("persistence_max_events", 100_000),
                "repository.persistence_max_events",
                integer=True,
            )
        ),
    )
