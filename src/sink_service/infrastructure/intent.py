"""Persist operator stop intent across Sink Service restarts."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class SinkIntentStore:
    """Store only explicitly stopped Sink identities; default is enabled."""

    def __init__(self, path: Path) -> None:
        self._path = path
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list) or any(not isinstance(x, str) for x in loaded):
                raise ValueError("invalid Sink intent state")
            self._stopped = set(loaded)
        else:
            self._stopped: set[str] = set()

    def should_start(self, sink_id: str) -> bool:
        """Return false for explicitly stopped Sinks."""
        return sink_id not in self._stopped

    def set_running(self, sink_id: str, running: bool) -> None:
        """Atomically persist operator intent before applying lifecycle changes."""
        updated = set(self._stopped)
        if running:
            updated.discard(sink_id)
        else:
            updated.add(sink_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".sink-state-", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(sorted(updated), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self._path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        self._stopped = updated
