"""Bounded JSONL truth recording with directory traversal protection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from data_generator.domain.model import GeneratorError
from data_generator.domain.scenario import Settings


class JsonlRecordWriter:
    @staticmethod
    def _append(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")

    async def append(
        self, settings: Settings, generation_id: str, payload: dict[str, object]
    ) -> None:
        options = settings.service.get("recording", {})
        base = settings.root.parent.resolve()
        directory = (base / options["directory"]).resolve()
        if not directory.is_relative_to(base):
            raise GeneratorError("recording directory escapes project root")
        destination = directory / f"{generation_id}.jsonl"
        await asyncio.to_thread(self._append, destination, payload)
