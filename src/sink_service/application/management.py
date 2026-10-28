"""Sink management operations, independent from Collector."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from sink_service.application.runtime import SinkRuntime


class SinkManager:
    """Own management commands for the Sink Service process."""

    def __init__(
        self,
        sinks: Mapping[str, SinkRuntime],
        *,
        config_digest: Callable[[], str],
        applied_digest: str,
    ) -> None:
        self._sinks = dict(sinks)
        self._config_digest = config_digest
        self._applied_digest = applied_digest

    def config_pending_restart(self) -> bool:
        """Report an unapplied disk configuration; never imply hot reload."""
        try:
            return self._config_digest() != self._applied_digest
        except OSError:
            return True

    def service_status(self) -> dict[str, object]:
        """Return aggregate lifecycle status."""
        return {
            "count": len(self._sinks),
            "running": sum(s.state.value == "running" for s in self._sinks.values()),
            "failed": sum(s.state.value == "failed" for s in self._sinks.values()),
            "config_pending_restart": self.config_pending_restart(),
        }

    def list_sinks(self) -> list[dict[str, object]]:
        """Return all Sink lifecycle snapshots."""
        return [self.get_sink_status(name) for name in sorted(self._sinks)]

    def get_sink_status(self, name: str) -> dict[str, object]:
        """Read a Sink without opening its output connection."""
        sink = self._sinks[name]
        return {
            "name": name,
            "state": sink.state.value,
            "desired_running": sink.desired_running,
            "failure": sink.failure,
            "nats_connected": sink.nats_connected,
            "config_pending_restart": self.config_pending_restart(),
            "last_written": sink.last_written.isoformat() if sink.last_written else None,
        }

    async def start_sink(self, name: str) -> dict[str, object]:
        """Persist and apply explicit start."""
        await self._sinks[name].start(explicit=True)
        return self.get_sink_status(name)

    async def stop_sink(self, name: str) -> dict[str, object]:
        """Persist and apply explicit stop."""
        await self._sinks[name].stop(explicit=True)
        return self.get_sink_status(name)

    async def restart_sink(self, name: str) -> dict[str, object]:
        """Explicitly restart one Sink."""
        await self._sinks[name].restart()
        return self.get_sink_status(name)

    async def clear_sink(self, name: str) -> dict[str, object]:
        """Clear stopped realtime cache only; never delete destination records."""
        sink = self._sinks[name]
        skipped = await sink.clear()
        return {**self.get_sink_status(name), "skipped": skipped}
