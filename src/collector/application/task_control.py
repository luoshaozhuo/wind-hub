"""Collector task control with durable runtime intent, never YAML mutation."""

from __future__ import annotations

import asyncio

from collector.application.config_models import SnapshotConflictError, SnapshotError
from collector.application.configuration import CollectorConfigurationManager, ConfigurationStatus
from collector.application.lifecycle import CollectorLifecycle
from collector.application.ports.task_intents import TaskIntentPort
from core.infrastructure.config import read_yaml


class CollectorTaskControl:
    """Persist accepted intent before safely reconfiguring device acquisition."""

    def __init__(
        self,
        configuration: CollectorConfigurationManager,
        lifecycle: CollectorLifecycle,
        intents: TaskIntentPort,
    ) -> None:
        self._configuration = configuration
        self._lifecycle = lifecycle
        self._intents = intents
        self._lock = asyncio.Lock()

    async def set_desired(
        self, device_id: str, desired_state: str, *, expected_revision: str
    ) -> ConfigurationStatus:
        """Apply device-specific task intent under a revision guard."""
        if desired_state not in {"START", "STOP"} or not device_id:
            raise SnapshotError("invalid Collector Task intent")
        async with self._lock:
            revision, _ = await asyncio.to_thread(self._configuration.get_disk)
            if not expected_revision or revision != expected_revision:
                raise SnapshotConflictError("Task configuration revision changed")
            paths = self._configuration.active_paths()
            if paths.tasks is None or paths.devices is None:
                raise SnapshotError("selected Collector Tasks/Devices unavailable")
            devices = read_yaml(paths.devices).get("devices", {})
            if device_id not in devices or devices[device_id].get("enabled", True) is False:
                raise LookupError("unknown or disabled Collector device")
            task_version = read_yaml(paths.tasks).get("file_version")
            if not isinstance(task_version, str) or not task_version:
                raise SnapshotError("invalid selected Task Instance version")
            await asyncio.to_thread(
                self._intents.write_intent,
                device_id,
                task_version,
                desired_state,
            )
            errors = await self._lifecycle.converge(paths, revision)
            current = self._configuration.status()
            return ConfigurationStatus(
                disk_revision=current.disk_revision,
                runtime_revision=current.runtime_revision,
                restart_required=current.restart_required,
                apply_errors={**current.apply_errors, **errors},
            )
