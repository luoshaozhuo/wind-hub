"""Strict Commander-owned service settings; never hot-applied."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from commander.application.settings import ServiceSettings, SimulationGatewayTarget
from core.configuration.local_selection import select_local_service
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
    """Validate the token-only Commander gRPC contract, without reading secrets."""
    if "grpc" not in raw and "security" not in raw:
        return
    grpc = _mapping(raw.get("grpc"), "grpc")
    security = _mapping(raw.get("security"), "security")
    _keys(grpc, {"listen_host", "listen_port", "max_active_rpcs"}, "grpc")
    _keys(security, {"callers", "recovery"}, "security")
    if grpc.get("listen_host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("grpc.listen_host must be loopback")
    port = grpc.get("listen_port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("grpc.listen_port must be in 1..65535")
    max_rpcs = grpc.get("max_active_rpcs")
    if type(max_rpcs) is not int or max_rpcs < 1:
        raise ValueError("grpc.max_active_rpcs must be positive")
    callers = security.get("callers")
    if not isinstance(callers, list) or not callers:
        raise ValueError("security.callers must be a nonempty list")
    seen_ids: set[str] = set()
    seen_refs: set[str] = set()
    for index, candidate in enumerate(callers):
        caller = _mapping(candidate, f"security.callers[{index}]")
        _keys(caller, {"caller_id", "token_env", "permissions"}, "caller")
        caller_id = caller.get("caller_id")
        reference = caller.get("token_env")
        permissions = caller.get("permissions")
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise ValueError("caller_id is required")
        if not isinstance(reference, str) or re.fullmatch(r"[A-Z_][A-Z0-9_]*", reference) is None:
            raise ValueError("caller token_env must name a secret environment variable")
        if caller_id in seen_ids or reference in seen_refs:
            raise ValueError("duplicate caller_id or token_env")
        if (
            not isinstance(permissions, list)
            or not permissions
            or any(
                not isinstance(scope, str) or scope not in {"query", "control", "reload", "recover"}
                for scope in permissions
            )
            or len(permissions) != len(set(permissions))
        ):
            raise ValueError("invalid caller permissions")
        seen_ids.add(caller_id)
        seen_refs.add(reference)


def load_service_settings(path: str | Path) -> ServiceSettings:
    """Validate the v1_1 Commander service configuration; never hot-apply it."""
    raw = read_yaml(Path(path))
    _keys(
        raw,
        {
            "schema_version",
            "file_version",
            "service_id",
            "config_sources",
            "grpc",
            "security",
            "dispatcher",
            "repository",
            "audit",
            "timeouts",
            "shutdown",
            "simulation_targets",
        },
        "commander service",
    )
    if type(raw.get("schema_version")) is not int or raw["schema_version"] not in {2, 3}:
        raise ValueError("unsupported Commander service schema_version")
    file_version = raw.get("file_version")
    if (
        not isinstance(file_version, str)
        or re.fullmatch(r"(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)", file_version)
        is None
    ):
        raise ValueError("commander file_version must be MAJOR.MINOR.PATCH")
    selected = select_local_service(Path(path))
    if set(selected.instances) != {"devices"}:
        raise ValueError("Commander must select exactly the devices Instance")
    devices_path = selected.instances["devices"]
    templates_root = selected.templates
    simulation = _mapping(raw.get("simulation_targets", {}), "simulation_targets")
    simulation_targets: dict[str, SimulationGatewayTarget] = {}
    for device_id, raw_target in simulation.items():
        if not device_id.strip():
            raise ValueError("simulation device_id cannot be blank")
        target = _mapping(raw_target, f"simulation_targets.{device_id}")
        _keys(
            target,
            {"gateway_id", "endpoint", "token_env"},
            f"simulation_targets.{device_id}",
        )
        fields = ("gateway_id", "endpoint", "token_env")
        if any(
            not isinstance(target.get(field), str) or not target[field].strip() for field in fields
        ):
            raise ValueError(f"simulation_targets.{device_id}: required field missing")
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", target["token_env"]) is None:
            raise ValueError(f"simulation_targets.{device_id}: invalid token_env")
        endpoint = target["endpoint"]
        host, separator, port = endpoint.rpartition(":")
        if (
            not separator
            or host not in {"127.0.0.1", "localhost", "::1"}
            or not port.isdecimal()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError(f"simulation_targets.{device_id}: invalid loopback endpoint")
        simulation_targets[device_id] = SimulationGatewayTarget(
            gateway_id=target["gateway_id"],
            endpoint=endpoint,
            token_env=target["token_env"],
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
            "accept_commit_timeout_ms",
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
        simulation_targets=simulation_targets,
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
        accept_commit_timeout=(
            float(
                _positive(
                    repository.get("accept_commit_timeout_ms", 10000),
                    "repository.accept_commit_timeout_ms",
                )
            )
            / 1000
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
