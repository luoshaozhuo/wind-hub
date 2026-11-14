"""Append-only truth recording output port."""

from __future__ import annotations

from typing import Protocol

from data_generator.domain.scenario import Settings


class RecordingPort(Protocol):
    async def append(
        self, settings: Settings, generation_id: str, payload: dict[str, object]
    ) -> None: ...
