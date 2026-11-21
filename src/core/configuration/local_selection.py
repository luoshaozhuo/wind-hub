"""Resolve immutable Deployment selections for service bootstrap.

No service may infer a latest Instance, scan a Template version, or select
another Deployment when its explicitly selected dependency is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.configuration.deployment import (
    INSTANCE_ROLE_TYPES,
    parse_deployment,
    service_instance_refs,
)
from core.infrastructure.config import read_yaml

_INSTANCE_PREFIXES = {
    "site": "site",
    "device": "device",
    "collector_tasks": "collector_tasks",
    "scenario": "scenarios",
    "sink": "sinks",
    "delivery": "deliveries",
}


@dataclass(frozen=True, slots=True)
class LocalSelection:
    """Validated paths for one Deployment-selected Service."""

    deployment_id: str
    service_id: str
    deployment_path: Path
    service_path: Path
    instances: dict[str, Path]
    templates: Path


def _safe_file(root: Path, relative: Path) -> Path:
    """Reject missing, escaping, and symlinked configuration selections."""
    path = root / relative
    if not path.is_relative_to(root):
        raise ValueError("selected configuration escapes its root")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("symbolic-link configuration references are forbidden")
        current = current.parent
    if not path.is_file():
        raise ValueError(f"selected configuration is missing: {relative}")
    return path


def select_local_service(
    service_path: Path,
    *,
    deployment_id: str | None = None,
) -> LocalSelection:
    """Select one exact Service/Instance version using the Deployment manifest.

    Args:
        service_path: Versioned Service YAML inside configs/services.
        deployment_id: Explicit deployment identity, or the environment override.

    Returns:
        Validated immutable selected configuration paths.

    Raises:
        ValueError: Selection is missing, ambiguous, unsafe, or inconsistent.
    """
    service_path = service_path.absolute()
    if service_path.parent.name != "services":
        raise ValueError("Service must reside in configs/services")
    root = service_path.parent.parent
    if root.is_symlink() or not root.is_dir():
        raise ValueError("configuration root must be an ordinary directory")
    service_path = _safe_file(root, Path("services") / service_path.name)
    raw = read_yaml(service_path)
    if not isinstance(raw, dict):
        raise ValueError("Service configuration must be a mapping")
    service_id = raw.get("service_id")
    file_version = raw.get("file_version")
    if not isinstance(service_id, str) or not isinstance(file_version, str):
        raise ValueError("Service identity and version are required")
    selected_id = deployment_id or os.environ.get("WIND_HUB_DEPLOYMENT_ID")
    if selected_id is not None and (
        not isinstance(selected_id, str)
        or not selected_id
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in selected_id)
    ):
        raise ValueError("invalid deployment identity")
    deployments = root / "deployments"
    if deployments.is_symlink() or not deployments.is_dir():
        raise ValueError("Deployment directory is absent or unsafe")
    matches: list[tuple[Path, Any]] = []
    for path in sorted(deployments.glob("deployment_*_v*.yaml")):
        checked = _safe_file(root, path.relative_to(root))
        payload = read_yaml(checked)
        if not isinstance(payload, dict):
            raise ValueError("Deployment must be a mapping")
        selected = parse_deployment(payload)
        if selected.deployment_id != selected_id and selected_id is not None:
            continue
        service = selected.services.get(service_id)
        if service is not None:
            if service.file_version != file_version:
                if selected_id is not None:
                    raise ValueError("Service file differs from selected Deployment version")
                continue
            matches.append((checked, selected))
    if len(matches) != 1:
        raise ValueError("exactly one Deployment must select this Service version")
    manifest_path, deployment = matches[0]
    role_refs = service_instance_refs(raw, INSTANCE_ROLE_TYPES)
    source_roles = raw.get("config_sources", {}).get("instances", {})
    if not isinstance(source_roles, dict):
        raise ValueError("Service Instance role mapping is invalid")
    instances: dict[str, Path] = {}
    for role, instance_id in source_roles.items():
        if (INSTANCE_ROLE_TYPES[role], instance_id) not in role_refs:
            raise ValueError("invalid Instance role reference")
        config_type = INSTANCE_ROLE_TYPES[role]
        selection = deployment.instances.get((config_type, instance_id))
        if selection is None:
            raise ValueError(f"Deployment lacks Instance selection: {role}")
        prefix = _INSTANCE_PREFIXES[config_type]
        filename = f"{prefix}_{instance_id}_v{selection.file_version}.yaml"
        instance_path = _safe_file(root, Path("instances") / filename)
        instance = read_yaml(instance_path)
        if not isinstance(instance, dict) or instance.get("file_version") != selection.file_version:
            raise ValueError(f"Instance header does not match selected version: {role}")
        instances[role] = instance_path
    templates = root / "templates"
    if templates.is_symlink() or not templates.is_dir():
        raise ValueError("Template directory is absent or unsafe")
    return LocalSelection(
        deployment.deployment_id, service_id, manifest_path, service_path, instances, templates
    )
