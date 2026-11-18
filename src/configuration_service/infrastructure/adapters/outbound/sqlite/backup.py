"""Controlled, verifiable SQLite online backups for Configuration Service."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)


class SQLiteBackupManager:
    """Create one durable backup without accepting arbitrary destination paths."""

    def __init__(self, repository: SQLiteRepository, backup_root: Path) -> None:
        if not backup_root.is_absolute():
            raise ValueError("backup_root must be an absolute path")
        self._repository = repository
        self._backup_root = backup_root

    def recover(self) -> int:
        """Mark interrupted jobs and remove only orphaned temporary backup files."""
        interrupted = self._repository.recover_interrupted_backups()
        if self._backup_root.is_dir() and not self._backup_root.is_symlink():
            for path in self._backup_root.glob(".*.*.part"):
                parts = path.name.split(".")
                if (
                    len(parts) == 4
                    and parts[0] == ""
                    and len(parts[1]) == 32
                    and len(parts[2]) == 32
                    and all(char in "0123456789abcdef" for char in parts[1] + parts[2])
                    and parts[3] == "part"
                ):
                    path.unlink(missing_ok=True)
        return interrupted

    def run(self, task_id: str) -> None:
        """Run a previously persisted QUEUED task using SQLite Online Backup API."""
        if len(task_id) != 32 or any(char not in "0123456789abcdef" for char in task_id):
            raise ValueError("invalid backup task identifier")
        self._repository.start_backup_task(task_id)
        output: Path | None = None
        temporary: Path | None = None
        output_created = False
        try:
            self._backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._backup_root.is_symlink():
                raise RuntimeError("backup directory must not be a symlink")
            if os.name == "posix":
                self._backup_root.chmod(0o700)
            output = self._backup_root / f"{task_id}.sqlite3"
            if output.exists() or output.is_symlink():
                raise RuntimeError("backup destination already exists")
            temporary = self._backup_root / f".{task_id}.{uuid.uuid4().hex}.part"
            self._repository.backup_to(temporary)
            schema_version = self._verify(temporary)
            digest = self._sha256(temporary)
            size = temporary.stat().st_size
            temporary.replace(output)
            output_created = True
            temporary = None
            self._repository.finish_backup_task(
                task_id,
                path=str(output),
                size_bytes=size,
                sha256=digest,
                error=None,
                database_schema_version=schema_version,
                integrity_verified=True,
            )
        except Exception as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if output_created and output is not None:
                output.unlink(missing_ok=True)
            self._repository.finish_backup_task(
                task_id, path=None, size_bytes=None, sha256=None, error=str(exc)
            )
            raise

    @staticmethod
    def _verify(path: Path) -> int:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("backup foreign key check failed")
            meta = connection.execute("SELECT schema_version FROM db_meta WHERE id = 1").fetchone()
            pragma = connection.execute("PRAGMA user_version").fetchone()
            if meta is None or pragma is None or meta[0] != pragma[0]:
                raise RuntimeError("backup schema metadata mismatch")
            return int(meta[0])
        finally:
            connection.close()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
