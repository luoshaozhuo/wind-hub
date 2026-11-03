"""Collector runtime and configuration lifecycle ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .acquisition import AcquisitionPlan, CollectorAcquisition
from .config_models import SnapshotPaths


class Lifecycle(StrEnum):
    NEW = "NEW"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ConfiguredRuntime:
    revision: str
    site_id: str
    device_ids: tuple[str, ...]
    acquisition: AcquisitionPlan | None = None


class CollectorLifecycle:
    """Serializes startup, shutdown and configuration model replacement."""

    def __init__(
        self,
        parse: Callable[[SnapshotPaths, str], ConfiguredRuntime],
        acquisition: CollectorAcquisition | None = None,
    ) -> None:
        self._parse = parse
        self._acquisition = acquisition
        self._lock = asyncio.Lock()
        self._state = Lifecycle.NEW
        self._config: ConfiguredRuntime | None = None

    @property
    def state(self) -> Lifecycle:
        return self._state

    @property
    def configured(self) -> ConfiguredRuntime | None:
        return self._config

    @property
    def apply_errors(self) -> dict[str, str]:
        return self._acquisition.errors if self._acquisition is not None else {}

    async def start(self, paths: SnapshotPaths, revision: str) -> None:
        async with self._lock:
            if self._state is Lifecycle.RUNNING:
                return
            if self._state not in {Lifecycle.NEW, Lifecycle.STOPPED}:
                raise RuntimeError(f"cannot start in {self._state}")
            self._state = Lifecycle.STARTING
            try:
                self._config = await asyncio.to_thread(self._parse, paths, revision)
                if self._acquisition is not None and self._config.acquisition is not None:
                    await self._acquisition.start(self._config.acquisition, revision=revision)
            except BaseException:
                self._state = Lifecycle.FAILED
                raise
            self._state = Lifecycle.RUNNING

    async def converge(self, paths: SnapshotPaths, revision: str) -> dict[str, str]:
        async with self._lock:
            if self._state is not Lifecycle.RUNNING:
                return {"collector": "runtime is not running"}
            try:
                candidate = await asyncio.to_thread(self._parse, paths, revision)
            except Exception as exc:
                return {"collector": str(exc)}
            if self._acquisition is not None and candidate.acquisition is not None:
                try:
                    errors = await self._acquisition.reconfigure(
                        candidate.acquisition, revision=revision
                    )
                    if errors:
                        return errors
                except Exception as exc:
                    return {"collector": str(exc)}
            self._config = candidate
            return {}

    async def stop(self) -> None:
        async with self._lock:
            if self._state in {Lifecycle.NEW, Lifecycle.STOPPED}:
                return
            self._state = Lifecycle.STOPPING
            if self._acquisition is not None:
                await self._acquisition.stop()
            self._config = None
            self._state = Lifecycle.STOPPED
