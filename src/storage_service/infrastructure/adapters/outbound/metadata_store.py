"""Crash-recoverable single-owner SQLite metadata and idempotent tasks."""

from __future__ import annotations

import fcntl
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, Self


@dataclass(frozen=True, slots=True)
class ObjectRegistration:
    """Physical name and full identity must never be silently rebound."""

    name: str
    full_sha256: str
    identity_json: str
    schema_fingerprint: str
    state: Literal["CREATING", "READY"]


class MetadataConflict(RuntimeError):
    """Existing durable ownership or request-id arguments conflict."""


class MetadataStore:
    """One SQLite owner, fenced by an exclusive OS lock for the process lifetime."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("metadata path must be absolute")
        self._path = path
        self._lock_fd: int | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        if self._connection is not None:
            raise RuntimeError("metadata store is already open")
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = self._path.with_suffix(self._path.suffix + ".lock").open("a+b")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            connection = sqlite3.connect(self._path, isolation_level=None, timeout=10.0)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                self._create_schema(connection)
            except BaseException:
                connection.close()
                raise
        except BaseException:
            fd.close()
            raise
        self._lock_fd = fd.fileno()
        self._lock_file = fd
        self._connection = connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()
        if version is None or version[0] not in (0, 1):
            raise MetadataConflict("unsupported SQLite metadata schema")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_objects (
                name TEXT PRIMARY KEY,
                full_sha256 TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('CREATING', 'READY'))
            );
            CREATE TABLE IF NOT EXISTS blocked_partitions (
                topic TEXT NOT NULL,
                partition_id INTEGER NOT NULL,
                offset_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                PRIMARY KEY (topic, partition_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL,
                request_sha256 TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('EXPORT', 'BACKUP')),
                state TEXT NOT NULL,
                artifact_id TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT ''
            );
            PRAGMA user_version=1;
            """
        )

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("metadata store is not open")
        return self._connection

    def reserve_object(
        self, name: str, full_sha256: str, identity_json: str, schema_fingerprint: str
    ) -> ObjectRegistration:
        """Reserve before DDL; CREATING must be reconciled on restart."""
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            record = db.execute(
                """SELECT full_sha256, identity_json, schema_fingerprint, state
                   FROM storage_objects WHERE name = ?""",
                (name,),
            ).fetchone()
            if record is None:
                db.execute(
                    """INSERT INTO storage_objects
                       (name, full_sha256, identity_json, schema_fingerprint, state)
                       VALUES (?, ?, ?, ?, 'CREATING')""",
                    (name, full_sha256, identity_json, schema_fingerprint),
                )
                state: Literal["CREATING", "READY"] = "CREATING"
            else:
                if record[:3] != (full_sha256, identity_json, schema_fingerprint):
                    raise MetadataConflict(f"physical name collision: {name}")
                state = record[3]
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return ObjectRegistration(name, full_sha256, identity_json, schema_fingerprint, state)

    def mark_ready(self, registration: ObjectRegistration, *, verified: bool) -> None:
        """Only call after live TDengine schema and ownership have been checked."""
        if not verified:
            raise ValueError("live TDengine verification is mandatory")
        db = self._db()
        cursor = db.execute(
            """UPDATE storage_objects SET state = 'READY'
               WHERE name = ? AND full_sha256 = ?
                 AND identity_json = ? AND schema_fingerprint = ?""",
            (
                registration.name, registration.full_sha256,
                registration.identity_json, registration.schema_fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise MetadataConflict("reservation changed or disappeared")

    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, digest: str
    ) -> None:
        """Persist a stop-the-world point for this partition; never auto-skip."""
        self._db().execute(
            """INSERT INTO blocked_partitions
               (topic, partition_id, offset_id, reason, payload_sha256)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (topic, partition_id) DO UPDATE SET
               offset_id=excluded.offset_id, reason=excluded.reason,
               payload_sha256=excluded.payload_sha256""",
            (topic, partition, offset, reason, digest),
        )

    def get_blocked_partition(self, topic: str, partition: int) -> tuple[int, str] | None:
        row = self._db().execute(
            """SELECT offset_id, reason FROM blocked_partitions
               WHERE topic = ? AND partition_id = ?""",
            (topic, partition),
        ).fetchone()
        return (int(row[0]), str(row[1])) if row is not None else None

    def clear_blocked_partition(
        self, topic: str, partition: int, *, expected_offset: int
    ) -> bool:
        cursor = self._db().execute(
            """DELETE FROM blocked_partitions
               WHERE topic = ? AND partition_id = ? AND offset_id = ?""",
            (topic, partition, expected_offset),
        )
        return cursor.rowcount == 1

    def register_task(
        self, *, task_id: str, request_id: str, request_sha256: str, kind: str
    ) -> str:
        """Create idempotently: same request ID with different content is fatal."""
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT task_id, request_sha256, kind FROM tasks WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row[1:] != (request_sha256, kind):
                    raise MetadataConflict("request_id reused with different parameters")
                result = str(row[0])
            else:
                db.execute(
                    """INSERT INTO tasks
                       (task_id, request_id, request_sha256, kind, state)
                       VALUES (?, ?, ?, ?, 'QUEUED')""",
                    (task_id, request_id, request_sha256, kind),
                )
                result = task_id
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return result

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        fd = getattr(self, "_lock_file", None)
        if fd is not None:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()
            self._lock_fd = None
            self._lock_file = None
