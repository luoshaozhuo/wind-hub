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


# Mainline template categories, including the task template family.
_TEMPLATE_TYPES = {
    "point_tables": ("point_table", "point_table_"),
    "device_models": ("device_model", "device_model_"),
    "protocols": ("protocol", "protocol_"),
    "tasks": ("task", "task_"),
    "sinks": ("sink", "sink_"),
    "deliveries": ("delivery", "delivery_"),
}
# Mainline unversioned instance files, including simulated devices.
_INSTANCE_TYPES = {
    "devices": "device",
    "devices_sim": "device",
    "site": "site",
    "sinks": "sink",
    "deliveries": "delivery",
}
# Versioned instance files selected by Deployment closures.
_VERSIONED_INSTANCE_PREFIXES = {
    "device_": "device",
    "site_": "site",
    "sinks_": "sink",
    "deliveries_": "delivery",
    "collector_tasks_": "collector_tasks",
    "scenarios_": "scenario",
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
    # Versioned deployment files carry identities independent of their path.
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
        # Versioned Deployment-selected instances are an additional accepted
        # form; the mainline unversioned instance convention stays valid.
        for prefix, kind in sorted(
            _VERSIONED_INSTANCE_PREFIXES.items(), key=lambda item: -len(item[0])
        ):
            if name.startswith(prefix) and name.endswith(marker):
                config_id = name[len(prefix) : -len(marker)]
                if not config_id:
                    raise ValueError("empty versioned Instance identity")
                return PublicationIdentity(kind, config_id, version)
        unversioned_name = name.removesuffix(".yaml")
        instance_type = _INSTANCE_TYPES.get(unversioned_name)
        if instance_type is None:
            raise ValueError(f"unsupported instance file: {relative_path}")
        return PublicationIdentity(instance_type, unversioned_name, version)
    if len(parts) == 2 and parts[0] == "services":
        name = parts[1].removesuffix(".yaml")
        if name.startswith("service_configuration_v"):
            raise ValueError("Configuration bootstrap must be outside publication root")
        version_suffix = f"_v{version}"
        # Versioned service files must match their declared service identity;
        # the mainline unversioned service convention remains accepted.
        if name.startswith("service_") and name.endswith(version_suffix):
            config_id = name[len("service_") : -len(version_suffix)]
            if not config_id or document.get("service_id") != config_id:
                raise ValueError("Service filename/id mismatch")
            return PublicationIdentity("service", config_id, version)
        return PublicationIdentity("service", name, version)
    # Shared documents accept both the mainline unversioned names and their
    # versioned Deployment-closure form.
    if relative_path == "templates/business_points_standard.yaml":
        return PublicationIdentity("business_points", "standard", version)
    if relative_path == f"templates/business_points_standard_v{version}.yaml":
        return PublicationIdentity("business_points", "standard", version)
    if relative_path == "templates/units_standard.yaml":
        return PublicationIdentity("units", "standard", version)
    if relative_path == f"templates/units_standard_v{version}.yaml":
        return PublicationIdentity("units", "standard", version)
    if relative_path == f"templates/observability_v{version}.yaml":
        return PublicationIdentity("observability", "observability", version)
    raise ValueError(f"unsupported configuration file path: {relative_path}")
