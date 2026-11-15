"""YAML device-intent mutation adapter for Collector snapshot transactions."""

from __future__ import annotations

import yaml

from collector.application.config_models import SnapshotError


def mutate_device_intent(content: bytes, device_id: str, desired_state: str) -> bytes:
    """Update only the target device override, preserving other configuration."""
    document = yaml.safe_load(content)
    if not isinstance(document, dict):
        raise SnapshotError("device configuration must be a mapping")
    devices = document.get("devices")
    if not isinstance(devices, dict) or device_id not in devices:
        raise LookupError("unknown device")
    definition = devices[device_id]
    if not isinstance(definition, dict) or definition.get("enabled", True) is False:
        raise SnapshotError("device is disabled")
    overrides = definition.setdefault("overrides", {})
    if not isinstance(overrides, dict):
        raise SnapshotError("device overrides must be a mapping")
    task = overrides.setdefault("task", {})
    if not isinstance(task, dict):
        raise SnapshotError("device task override must be a mapping")
    task["desired_state"] = desired_state
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")
