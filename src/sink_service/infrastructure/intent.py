"""Durable per-Sink operator stop intent for process-isolated deployments."""

from __future__ import annotations

import os
import tempfile
from hashlib import sha256
from pathlib import Path


class SinkIntentStore:
    """One marker per Sink avoids cross-process read-modify-write races."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _marker(self, sink_id: str) -> Path:
        digest = sha256(sink_id.encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.stopped"

    def should_start(self, sink_id: str) -> bool:
        """Return false if an explicit Stop marker survives the last restart."""
        return not self._marker(sink_id).exists()

    def set_running(self, sink_id: str, running: bool) -> None:
        """Commit durable operator intent before executing lifecycle changes."""
        marker = self._marker(sink_id)
        if running:
            marker.unlink(missing_ok=True)
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".intent-", dir=marker.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(b"stopped\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
