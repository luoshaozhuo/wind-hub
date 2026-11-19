"""Validated local bootstrap YAML for Configuration Service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from configuration_service.infrastructure.adapters.outbound.yaml.codec import parse_yaml


@dataclass(frozen=True, slots=True)
class ServerSettings:
    service_id: str
    host: str
    port: int
    token_sources: dict[str, str]
    nats_servers: tuple[str, ...]
    sqlite_path: Path
    backup_root: Path
    cursor_secret_file: Path
    permissions: dict[str, frozenset[str]]
    max_request_bytes: int
    max_bundle_bytes: int
    max_dependencies: int
    max_documents_per_publication: int
    candidate_ttl_seconds: int
    max_candidates: int
    max_backup_jobs: int
    max_concurrent_queries: int
    page_size: int
    write_timeout_ms: int
    max_workers: int


def _mapping(raw: Any, context: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    return raw


def _text(raw: Any, context: str) -> str:
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise ValueError(f"{context} must be a nonempty trimmed string")
    return raw


def _number(raw: Any, context: str, minimum: int, maximum: int) -> int:
    if type(raw) is not int or not minimum <= raw <= maximum:
        raise ValueError(f"{context} must be an integer in [{minimum}, {maximum}]")
    return raw


def _file(raw: Any, context: str) -> Path:
    value = Path(_text(raw, context))
    if not value.is_absolute():
        raise ValueError(f"{context} must use an absolute path")
    return value


def load_settings(path: Path) -> ServerSettings:
    """Load the service's own YAML; no configuration database dependency."""
    if not path.is_absolute() or not path.is_file():
        raise ValueError("bootstrap YAML must be an existing absolute path")
    raw = parse_yaml(path.read_bytes(), max_bytes=1024 * 1024)
    if raw["schema_version"] != 1:
        raise ValueError("unsupported bootstrap schema_version")
    allowed_root = {
        "schema_version",
        "file_version",
        "service_id",
        "grpc",
        "repository",
        "limits",
        "security",
        "nats",
    }
    if set(raw) != allowed_root:
        raise ValueError("invalid bootstrap configuration root fields")
    grpc_config = _mapping(raw["grpc"], "grpc")
    repository = _mapping(raw["repository"], "repository")
    limits = _mapping(raw["limits"], "limits")
    security = _mapping(raw["security"], "security")
    nats_config = _mapping(raw["nats"], "nats")

    def assert_fields(value: dict[str, Any], allowed: set[str], name: str) -> None:
        unexpected = set(value) - allowed
        if unexpected:
            raise ValueError(f"unknown {name} fields: {sorted(unexpected)}")

    assert_fields(grpc_config, {"host", "port"}, "grpc")
    assert_fields(repository, {"sqlite_path", "backup_root"}, "repository")
    assert_fields(security, {"cursor_secret_file", "token_sources", "permissions"}, "security")
    assert_fields(nats_config, {"servers"}, "nats")
    servers = nats_config["servers"]
    if not isinstance(servers, list) or not servers or any(
        not isinstance(item, str) or not item.startswith("nats://") for item in servers
    ):
        raise ValueError("nats.servers must be nonempty nats:// endpoints")
    assert_fields(
        limits,
        {
            "max_request_bytes",
            "max_bundle_bytes",
            "max_dependencies",
            "max_documents_per_publication",
            "candidate_ttl_seconds",
            "page_size",
            "write_timeout_ms",
            "max_workers",
            "max_candidates",
            "max_backup_jobs",
            "max_concurrent_queries",
        },
        "limits",
    )
    perms = _mapping(security["permissions"], "security.permissions")
    identities: dict[str, frozenset[str]] = {}
    for actor, roles in perms.items():
        name = _text(actor, "client identity")
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in ("query", "publish", "admin") for role in roles)
        ):
            raise ValueError("client permissions must be a nonempty role list")
        identities[name] = frozenset(roles)
    if not identities:
        raise ValueError("at least one authenticated token principal is required")
    configured_sources = _mapping(security["token_sources"], "security.token_sources")
    sources: dict[str, str] = {}
    for actor, reference in configured_sources.items():
        principal = _text(actor, "token actor")
        name = _text(reference, "security token source")
        if re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", name) is None:
            raise ValueError("token source must reference a controlled environment variable")
        sources[principal] = name.removeprefix("env:")
    if set(sources) != set(identities):
        raise ValueError("token source and permission principals must agree")
    return ServerSettings(
        service_id=_text(raw["service_id"], "service_id"),
        host=_text(grpc_config["host"], "grpc.host"),
        port=_number(grpc_config["port"], "grpc.port", 1, 65535),
        token_sources=sources,
        nats_servers=tuple(servers),
        sqlite_path=_file(repository["sqlite_path"], "repository.sqlite_path"),
        backup_root=_file(repository["backup_root"], "repository.backup_root"),
        cursor_secret_file=_file(security["cursor_secret_file"], "security.cursor_secret_file"),
        permissions=identities,
        max_request_bytes=_number(
            limits["max_request_bytes"], "max_request_bytes", 1024, 64 * 1024 * 1024
        ),
        max_bundle_bytes=_number(
            limits["max_bundle_bytes"], "max_bundle_bytes", 1024, 64 * 1024 * 1024
        ),
        max_dependencies=_number(limits["max_dependencies"], "max_dependencies", 1, 100_000),
        max_documents_per_publication=_number(
            limits["max_documents_per_publication"], "max_documents_per_publication", 1, 1000
        ),
        candidate_ttl_seconds=_number(
            limits["candidate_ttl_seconds"], "candidate_ttl_seconds", 1, 86400
        ),
        max_candidates=_number(limits["max_candidates"], "max_candidates", 1, 100_000),
        max_backup_jobs=_number(limits["max_backup_jobs"], "max_backup_jobs", 1, 64),
        max_concurrent_queries=_number(
            limits["max_concurrent_queries"], "max_concurrent_queries", 1, 1024
        ),
        page_size=_number(limits.get("page_size", 100), "page_size", 1, 1000),
        write_timeout_ms=_number(limits["write_timeout_ms"], "write_timeout_ms", 1, 120_000),
        max_workers=_number(limits.get("max_workers", 16), "max_workers", 1, 256),
    )
