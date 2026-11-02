"""Reader synchronization against atomic, immutable configuration generations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from sink_service.application.config import read_snapshot
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot

ApplyCallback = Callable[[ValidatedSnapshot], Awaitable[None]]


class ConfigReader:
    """Poll the committed pointer, applying only complete validated snapshots."""

    def __init__(
        self,
        root: Path,
        service_file: str,
        apply: ApplyCallback,
        *,
        poll_interval_ms: int,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ConfigurationError("poll interval must be positive")
        self._root = root
        self._service_file = service_file
        self._apply = apply
        self._interval = poll_interval_ms / 1000
        self._last_revision = ""
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_revision(self) -> str:
        return self._last_revision

    def _snapshot(self) -> ValidatedSnapshot:
        pointer = (self._root / "CURRENT").read_text(encoding="ascii").strip()
        if len(pointer) != 64 or any(d not in "0123456789abcdef" for d in pointer):
            raise ConfigurationError("invalid committed generation pointer")
        generation = self._root / ".generations" / pointer
        return read_snapshot(generation, generation / self._service_file)

    async def refresh(self) -> bool:
        """A failed application never advances the Reader's effective revision."""
        try:
            snapshot = await asyncio.to_thread(self._snapshot)
            if snapshot.revision == self._last_revision:
                return False
            await self._apply(snapshot)
        except (OSError, ConfigurationError, ValueError, RuntimeError) as exc:
            self._last_error = str(exc)
            return False
        self._last_revision = snapshot.revision
        self._last_error = ""
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Refresh until shutdown; one failure cannot silently stop polling."""
        while not stop.is_set():
            await self.refresh()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
