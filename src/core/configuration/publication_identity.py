"""Shared, strict configuration publication path and logical identity rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


def validate_relative_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or len(path.encode("utf-8")) > 512
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or PurePosixPath(path).as_posix() != path
        or not path.endswith(".yaml")
    ):
        raise ValueError("invalid configuration directory relative_path")


@dataclass(frozen=True, slots=True)
class PublicationIdentity:
    config_type: str
    config_id: str
    file_version: str


_TEMPLATE_TYPES = {
    "point_tables": ("point_table", "point_table_"),
    "device_models": ("device_model", "device_model_"),
    "protocols": ("protocol", "protocol_"),
    "tasks": ("task", "task_"),
    "sinks": ("sink", "sink_"),
    "deliveries": ("delivery", "delivery_"),
}
_INSTANCE_TYPES = {
    "devices": "device",
    "devices_sim": "device",
    "site": "site",
    "sinks": "sink",
    "deliveries": "delivery",
}
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def resolve_publication_identity(
    relative_path: str,
    document: dict[str, object],
) -> PublicationIdentity:
    """Map an explicitly supported configuration path to its stable identity."""
    validate_relative_path(relative_path)
    version = document.get("file_version")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError(f"invalid file_version: {relative_path}")
    parts = relative_path.split("/")
    if len(parts) == 3 and parts[0] == "templates":
        category = _TEMPLATE_TYPES.get(parts[1])
        if category is None:
            raise ValueError(f"unsupported template directory: {relative_path}")
        config_type, prefix = category
        name = parts[2]
        marker = f"_v{version}.yaml"
        if not name.startswith(prefix) or not name.endswith(marker):
            raise ValueError(f"template path/version mismatch: {relative_path}")
        logical_id = name[len(prefix) : -len(marker)]
        if not logical_id or document.get("template_id") != logical_id:
            raise ValueError(f"template path/id mismatch: {relative_path}")
        return PublicationIdentity(config_type, logical_id, version)
    # Versioned deployment and business files have stable identities independent of paths.
    if len(parts) == 2 and parts[0] == "deployments":
        name = parts[1]
        marker = f"_v{version}.yaml"
        if not name.startswith("deployment_") or not name.endswith(marker):
            raise ValueError("Deployment filename/version mismatch")
        config_id = name[len("deployment_") : -len(marker)]
        if not config_id or document.get("deployment_id") != config_id:
            raise ValueError("Deployment filename/id mismatch")
        return PublicationIdentity("deployment", config_id, version)
    if len(parts) == 2 and parts[0] == "instances":
        name = parts[1]
        marker = f"_v{version}.yaml"
        prefixes = {
            "device_": "device",
            "site_": "site",
            "sinks_": "sink",
            "deliveries_": "delivery",
            "collector_tasks_": "collector_tasks",
        }
        for prefix, kind in sorted(prefixes.items(), key=lambda item: -len(item[0])):
            if name.startswith(prefix) and name.endswith(marker):
                config_id = name[len(prefix) : -len(marker)]
                if not config_id:
                    raise ValueError("empty versioned Instance identity")
                return PublicationIdentity(kind, config_id, version)
        name = parts[1].removesuffix(".yaml")
        instance_type = _INSTANCE_TYPES.get(name)
        if instance_type is None:
            raise ValueError(f"unsupported instance file: {relative_path}")
        return PublicationIdentity(instance_type, name, version)
    if len(parts) == 2 and parts[0] == "services":
        name = parts[1]
        marker = f"_v{version}.yaml"
        if name.startswith("service_") and name.endswith(marker):
            config_id = name[len("service_") : -len(marker)]
            if not config_id or document.get("service_id") != config_id:
                raise ValueError("Service filename/id mismatch")
            return PublicationIdentity("service", config_id, version)
        name = parts[1].removesuffix(".yaml")
        if name.startswith("service_configuration_v"):
            raise ValueError("Configuration bootstrap must be outside publication root")
        return PublicationIdentity("service", name, version)
    if relative_path == "templates/business_points_standard.yaml":
        return PublicationIdentity("business_points", "standard", version)
    if relative_path == "templates/units_standard.yaml":
        return PublicationIdentity("units", "standard", version)
    if relative_path == f"templates/observability_v{version}.yaml":
        return PublicationIdentity("observability", "observability", version)
    raise ValueError(f"unsupported configuration file path: {relative_path}")
