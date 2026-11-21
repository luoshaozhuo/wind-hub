"""Resolve Sink Instance identities within a verified selected closure."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from sink_service.domain.config import ConfigurationError, expect_mapping


def validate_logical_sources(service: dict[str, Any]) -> dict[str, str]:
    """Validate that a Service names only its Sink and Delivery Instances."""
    sources = expect_mapping(service.get("config_sources"), "service.config_sources")
    if set(sources) != {"instances"}:
        raise ConfigurationError("Service may depend only on Instance identities")
    instances = expect_mapping(sources["instances"], "config_sources.instances")
    if set(instances) != {"sinks", "deliveries"}:
        raise ConfigurationError("Service requires Sink and Delivery Instance identities")
    for role, identifier in instances.items():
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", identifier
        ):
            raise ConfigurationError(f"invalid {role} Instance identity")
    return instances


def config_sources(
    service: dict[str, Any], available: Collection[str]
) -> tuple[dict[str, str], str]:
    """Resolve exact selected Instance documents, never choose a highest version."""
    instances = validate_logical_sources(service)
    paths: dict[str, str] = {}
    for family, config_id in instances.items():
        prefix = f"configs/instances/{family}_{config_id}_v"
        matches = [
            path
            for path in available
            if path.startswith(prefix) and re.fullmatch(r"\d+\.\d+\.\d+\.yaml", path[len(prefix) :])
        ]
        if len(matches) != 1:
            raise ConfigurationError(f"{family}: ambiguous or missing selected Instance version")
        paths[family] = matches[0]
    return paths, "configs/templates"
