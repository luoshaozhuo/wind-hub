"""Validate Sink configuration source references at the application boundary."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from sink_service.domain.config import (
    ConfigurationError,
    expect_mapping,
    expect_nonempty_str,
)


def managed_path(value: object, location: str, *, directory: bool = False) -> str:
    """Accept a single config-root-relative pathname; never infer a default."""
    path = expect_nonempty_str(value, location)
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in parts)
        or PurePosixPath(path).as_posix() != path
        or len(parts) < 2
        or parts[0] != "configs"
        or (not directory and not path.endswith(".yaml"))
    ):
        raise ConfigurationError(f"{location}: unsafe config-root-relative pathname")
    return path


def config_sources(service: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return explicit Instance file paths and the Template root."""
    sources = expect_mapping(service.get("config_sources"), "service.config_sources")
    if set(sources) != {"instances", "templates"}:
        raise ConfigurationError("config_sources requires instances and templates")
    instances = expect_mapping(sources["instances"], "config_sources.instances")
    if set(instances) != {"sinks", "deliveries"}:
        raise ConfigurationError("config_sources.instances requires sinks and deliveries")
    paths = {
        key: managed_path(value, f"config_sources.instances.{key}")
        for key, value in instances.items()
    }
    if len(set(paths.values())) != len(paths):
        raise ConfigurationError("config_sources Instance paths must differ")
    if any(not path.startswith("configs/instances/") for path in paths.values()):
        raise ConfigurationError("Instance pathname must reside under configs/instances/")
    templates = expect_mapping(sources["templates"], "config_sources.templates")
    if set(templates) != {"root"}:
        raise ConfigurationError("config_sources.templates requires root")
    root = managed_path(templates["root"], "config_sources.templates.root", directory=True)
    if not (root == "configs/templates" or root.startswith("configs/templates/")):
        raise ConfigurationError("Template root must reside under configs/templates/")
    if root in paths.values() or any(path.startswith(root + "/") for path in paths.values()):
        raise ConfigurationError("config_sources Instance path overlaps Template root")
    return paths, root
