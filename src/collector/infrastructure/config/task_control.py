"""Collector-owned durable START/STOP intent updates.

Control changes only the device instance document, never a shared task
template. The full snapshot commit is the linearization point.
"""

from __future__ import annotations

import asyncio

import yaml

from collector.application.config_models import SnapshotConflictError, SnapshotError
from collector.application.configuration import (
    CollectorConfigurationManager,
    ConfigurationStatus,
)


class CollectorTaskControl:
    """Serialize intent mutations through the same configuration manager."""

    def __init__(self, configuration: CollectorConfigurationManager) -> None:
        self._configuration = configuration
        self._lock = asyncio.Lock()

    async def set_desired(
        self,
        device_id: str,
        desired_state: str,
        *,
        expected_revision: str,
    ) -> ConfigurationStatus:
        if desired_state not in {"START", "STOP"}:
            raise SnapshotError("invalid task desired_state")
        if not device_id:
            raise SnapshotError("device ID is required")
        async with self._lock:
            revision, files = self._configuration.get_disk()
            if revision != expected_revision:
                raise SnapshotConflictError("task revision has changed")
            path = "instances/devices.yaml"
            document = yaml.safe_load(files[path])
            devices = document.get("devices")
            if not isinstance(devices, dict) or device_id not in devices:
                raise LookupError("unknown device")
            definition = devices[device_id]
            if not isinstance(definition, dict) or definition.get("enabled", True) is False:
                raise SnapshotError("device is disabled")
            overrides = definition.setdefault("overrides", {})
            task = overrides.setdefault("task", {})
            task["desired_state"] = desired_state
            files[path] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode(
                "utf-8"
            )
            candidate_id, _ = await asyncio.to_thread(
                self._configuration.push,
                files,
                expected_revision=expected_revision,
            )
            return await self._configuration.apply(
                candidate_id, expected_revision=expected_revision
            )
