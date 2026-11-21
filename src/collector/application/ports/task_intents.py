"""Task-intent persistence port used by Collector Application."""

from __future__ import annotations

from typing import Protocol


class TaskIntentPort(Protocol):
    """Control intent is independent of immutable Device and Task YAML."""

    def read_intents(self, file_version: str) -> dict[str, str]: ...

    def write_intent(self, device_id: str, file_version: str, state: str) -> None: ...
