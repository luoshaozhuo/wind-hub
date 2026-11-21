"""Collector task control with durable runtime intent, never YAML mutation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from collector.application.config_models import SnapshotConflictError, SnapshotError
from collector.application.configuration import CollectorConfigurationManager, ConfigurationStatus
from collector.application.lifecycle import CollectorLifecycle
from collector.application.ports.task_intents import TaskIntentPort


class CollectorTaskControl:
    """Persist accepted intent before safely reconfiguring device acquisition."""

    def __init__(
        self,
        configuration: CollectorConfigurationManager,
        lifecycle: CollectorLifecycle,
        intents: TaskIntentPort,
        read_config: Callable[[Path], dict[str, Any]],
    ) -> None:
        self._configuration = configuration
        self._lifecycle = lifecycle
        self._intents = intents
        self._read_config = read_config
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
            devices = self._read_config(paths.devices).get("devices", {})
            if device_id not in devices or devices[device_id].get("enabled", True) is False:
                raise LookupError("unknown or disabled Collector device")
            task_version = self._read_config(paths.tasks).get("file_version")
            if not isinstance(task_version, str) or not task_version:
                raise SnapshotError("invalid selected Task Instance version")
            task_identity = f"{paths.tasks.name}:{task_version}"
            await asyncio.to_thread(
                self._intents.write_intent,
                device_id,
                task_identity,
                desired_state,
            )
            current_revision, _ = await asyncio.to_thread(self._configuration.get_disk)
            if current_revision != revision:
                raise SnapshotConflictError("Task configuration changed while accepting intent")
            errors = await self._lifecycle.converge(paths, revision)
            current = self._configuration.status()
            return ConfigurationStatus(
                disk_revision=current.disk_revision,
                runtime_revision=current.runtime_revision,
                restart_required=current.restart_required,
                apply_errors={**current.apply_errors, **errors},
            )
