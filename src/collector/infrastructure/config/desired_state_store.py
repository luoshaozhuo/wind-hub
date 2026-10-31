"""Persist Collector task intent in the device instance document."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.application.errors import ConfigError
from core.infrastructure.config.yaml import (
    read_yaml_mapping,
    write_yaml_mapping_atomic,
)


class DesiredStateStore:
    """Serialize START/STOP writes and atomically replace devices.yaml."""

    def __init__(self, instances: str | Path) -> None:
        self._path = Path(instances) / "devices.yaml"
        self._lock = asyncio.Lock()

    async def set_state(self, device_id: str, state: str) -> None:
        if state not in ("START", "STOP"):
            raise ConfigError("invalid desired state")
        async with self._lock:
            document = read_yaml_mapping(self._path)
            devices = document.get("devices")
            if not isinstance(devices, dict) or device_id not in devices:
                raise ConfigError(f"device {device_id!r} not configured")
            device = devices[device_id]
            if not isinstance(device, dict):
                raise ConfigError(f"invalid device {device_id!r}")
            overrides = device.setdefault("overrides", {})
            if not isinstance(overrides, dict):
                raise ConfigError("device overrides must be mapping")
            task = overrides.setdefault("task", {})
            if not isinstance(task, dict):
                raise ConfigError("device task override must be mapping")
            if task.get("desired_state") == state:
                return
            task["desired_state"] = state
            write_yaml_mapping_atomic(self._path, document)
