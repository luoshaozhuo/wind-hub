"""Full configuration snapshots with bounded, validated candidate staging."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from data_generator.application.config import Settings, load_settings
from data_generator.domain.model import GeneratorError


@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    data: bytes


class SnapshotManager:
    """A single candidate is staged in memory, never changing disk on Push."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.candidate: tuple[str, tuple[FileContent, ...], str] | None = None
        self.max_files = 128
        self.max_bytes = 8 * 1024 * 1024

    def disk(self) -> tuple[FileContent, ...]:
        root = self.settings.root
        files = [root / "services" / "data-generator-service.yaml",
                 root / "instances" / "data-generator.yaml"]
        return tuple(FileContent(str(path.relative_to(root)), path.read_bytes())
                     for path in files)

    def get(self, scope: str, candidate_id: str = "") -> tuple[FileContent, ...]:
        if scope == "CANDIDATE":
            if self.candidate is None or candidate_id != self.candidate[0]:
                raise GeneratorError("candidate not found")
            return self.candidate[1]
        if scope in ("DISK", "RUNTIME"):
            return self.disk()
        raise GeneratorError("invalid snapshot scope")

    def push(self, files: tuple[FileContent, ...], expected: str) -> str:
        if expected and expected != self.settings.revision:
            raise GeneratorError("snapshot revision conflict")
        if not files or len(files) > self.max_files or sum(len(f.data) for f in files) > self.max_bytes:
            raise GeneratorError("snapshot capacity exceeded")
        paths = [f.path for f in files]
        if len(paths) != len(set(paths)):
            raise GeneratorError("duplicate snapshot path")
        allowed = {"services/data-generator-service.yaml",
                   "instances/data-generator.yaml"}
        if set(paths) != allowed:
            raise GeneratorError("snapshot must include exact service and instance files")
        for item in files:
            if not item.data:
                raise GeneratorError("empty snapshot file")
        candidate_id = str(uuid.uuid4())
        self.candidate = (candidate_id, files, self.settings.revision)
        return candidate_id

    def apply(self, candidate_id: str, expected: str) -> tuple[str, bool]:
        """Perform an atomic single-file update only; multi-file requires transactional storage."""
        if self.candidate is None or candidate_id != self.candidate[0]:
            raise GeneratorError("candidate not found")
        if expected and expected != self.settings.revision:
            raise GeneratorError("snapshot revision conflict")
        raise GeneratorError("multi-file atomic snapshot commit not implemented safely")


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
