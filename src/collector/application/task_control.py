"""Application use case: durable Collector task intent and convergence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from collector.application.config_models import SnapshotConflictError, SnapshotError
from collector.application.configuration import (
    CollectorConfigurationManager,
    ConfigurationStatus,
)

DeviceIntentMutator = Callable[[bytes, str, str], bytes]


class CollectorTaskControl:
    """Serialize task START/STOP intent through the configuration transaction."""

    def __init__(
        self,
        configuration: CollectorConfigurationManager,
        mutate_device_intent: DeviceIntentMutator,
    ) -> None:
        self._configuration = configuration
        self._mutate_device_intent = mutate_device_intent
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
            files[path] = self._mutate_device_intent(files[path], device_id, desired_state)
            candidate_id, _ = await asyncio.to_thread(
                self._configuration.push,
                files,
                expected_revision=expected_revision,
            )
            return await self._configuration.apply(
                candidate_id, expected_revision=expected_revision
            )
