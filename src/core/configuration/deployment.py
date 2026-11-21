"""Deployment-selected configuration versions and stable service-closure identities."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

INSTANCE_ROLE_TYPES: dict[str, str] = {
    "site": "site",
    "devices": "device",
    "tasks": "collector_tasks",
    "sinks": "sink",
    "deliveries": "delivery",
    "scenarios": "scenario",
}


@dataclass(frozen=True, slots=True)
class VersionSelection:
    config_type: str
    config_id: str
    file_version: str


@dataclass(frozen=True, slots=True)
class DeploymentSelection:
    deployment_id: str
    services: Mapping[str, VersionSelection]
    instances: Mapping[tuple[str, str], VersionSelection]


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a nonempty configuration identifier")
    return value


def _version(value: object, label: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError(f"{label} must be MAJOR.MINOR.PATCH")
    return value


def parse_deployment(document: Mapping[str, object]) -> DeploymentSelection:
    """Parse a deployment without inferring missing versions or defaults."""
    if set(document) != {
        "schema_version", "file_version", "deployment_id", "services", "instances"
    }:
        raise ValueError("invalid Deployment fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported Deployment schema_version")
    _version(document["file_version"], "Deployment.file_version")
    deployment_id = _identifier(document["deployment_id"], "deployment_id")
    raw_services = document["services"]
    raw_instances = document["instances"]
    if not isinstance(raw_services, dict) or not raw_services:
        raise ValueError("Deployment.services must be a nonempty mapping")
    if not isinstance(raw_instances, dict):
        raise ValueError("Deployment.instances must be a mapping")
    services: dict[str, VersionSelection] = {}
    for service_id, raw in raw_services.items():
        service_id = _identifier(service_id, "service_id")
        if not isinstance(raw, dict) or set(raw) != {"config_id", "file_version"}:
            raise ValueError(f"invalid Service selection: {service_id}")
        if _identifier(raw["config_id"], "service config_id") != service_id:
            raise ValueError("Service selection identity mismatch")
        services[service_id] = VersionSelection(
            "service", service_id, _version(raw["file_version"], "service.file_version")
        )
    instances: dict[tuple[str, str], VersionSelection] = {}
    for config_type, typed in raw_instances.items():
        config_type = _identifier(config_type, "instance config_type")
        if not isinstance(typed, dict):
            raise ValueError("Deployment instance type must contain a mapping")
        for config_id, raw in typed.items():
            config_id = _identifier(config_id, "instance config_id")
            if not isinstance(raw, dict) or set(raw) != {"file_version"}:
                raise ValueError("invalid Deployment Instance selection")
            key = (config_type, config_id)
            instances[key] = VersionSelection(
                config_type, config_id, _version(raw["file_version"], "instance.file_version")
            )
    return DeploymentSelection(deployment_id, services, instances)


def service_instance_refs(
    service: Mapping[str, object],
    role_types: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Return typed logical Instance references; never accept path-based sources."""
    sources = service.get("config_sources")
    if sources is None:
        return ()
    if not isinstance(sources, dict) or set(sources) != {"instances"}:
        raise ValueError("Service config_sources must contain instances only")
    refs = sources["instances"]
    if not isinstance(refs, dict):
        raise ValueError("Service.instances must be a mapping")
    result: list[tuple[str, str]] = []
    for role, config_id in refs.items():
        if role not in role_types:
            raise ValueError(f"unsupported Service Instance role: {role}")
        result.append((role_types[role], _identifier(config_id, f"instance role {role}")))
    if len(result) != len(set(result)):
        raise ValueError("Service references the same Instance more than once")
    return tuple(sorted(result))


def closure_sha256(
    deployment_id: str,
    service_id: str,
    documents: Iterable[tuple[str, str, str, str]],
) -> str:
    """Hash the immutable, effective document closure, independent of global revision."""
    identities = sorted(set(documents))
    if not identities:
        raise ValueError("service closure must not be empty")
    digest = hashlib.sha256()
    for value in (_identifier(deployment_id, "deployment_id"), _identifier(service_id, "service_id")):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    for kind, config_id, version, content_sha in identities:
        _identifier(kind, "config_type")
        _identifier(config_id, "config_id")
        _version(version, "file_version")
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha):
            raise ValueError("invalid content_sha256")
        for value in (kind, config_id, version, content_sha):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()
