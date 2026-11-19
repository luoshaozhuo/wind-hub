"""Crash-recoverable single-owner SQLite metadata and idempotent tasks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Self

from storage_service.domain.tasks import TaskRecord


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
        self._lock_file: BinaryIO | None = None
        self._connection: sqlite3.Connection | None = None
        self._mutex = threading.RLock()

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
            connection = sqlite3.connect(
                self._path, isolation_level=None, timeout=10.0, check_same_thread=False
            )
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
                payload BLOB NOT NULL,
                PRIMARY KEY (topic, partition_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL,
                request_sha256 TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('EXPORT', 'BACKUP')),
                wind_farm_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                artifact_id TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                finished_at TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS retry_requests (
                request_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                partition_id INTEGER NOT NULL,
                offset_id INTEGER NOT NULL
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
        with self._mutex:
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
        with self._mutex:
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
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        """Retain the original poison record; never replace an earlier blocker."""
        with self._mutex:
            import hashlib

            if not topic or partition < 0 or offset < 0 or len(payload) > 4 * 1024 * 1024:
                raise ValueError("invalid blocked partition evidence")
            digest = hashlib.sha256(payload).hexdigest()
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                previous = db.execute(
                    """SELECT offset_id, payload_sha256 FROM blocked_partitions
                       WHERE topic = ? AND partition_id = ?""",
                    (topic, partition),
                ).fetchone()
                if previous is not None and previous != (offset, digest):
                    raise MetadataConflict("cannot overwrite an earlier blocked Kafka record")
                if previous is None:
                    db.execute(
                        """INSERT INTO blocked_partitions
                           (topic, partition_id, offset_id, reason, payload_sha256, payload)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (topic, partition, offset, reason, digest, payload),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def get_blocked_partition(self, topic: str, partition: int) -> tuple[int, str] | None:
        with self._mutex:
            row = self._db().execute(
                """SELECT offset_id, reason FROM blocked_partitions
                   WHERE topic = ? AND partition_id = ?""",
                (topic, partition),
            ).fetchone()
            return (int(row[0]), str(row[1])) if row is not None else None

    def clear_blocked_partition(
        self, topic: str, partition: int, *, expected_offset: int
    ) -> bool:
        with self._mutex:
            cursor = self._db().execute(
                """DELETE FROM blocked_partitions
                   WHERE topic = ? AND partition_id = ? AND offset_id = ?""",
                (topic, partition, expected_offset),
            )
            return cursor.rowcount == 1

    def register_task(
        self, *, task_id: str, request_id: str, request_sha256: str, kind: str,
        wind_farm_id: str, owner_id: str
    ) -> str:
        """Create idempotently: same request ID with different content is fatal."""
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT task_id, request_sha256, kind, wind_farm_id, owner_id FROM tasks WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    if row[1:] != (request_sha256, kind, wind_farm_id, owner_id):
                        raise MetadataConflict("request_id reused with different parameters")
                    result = str(row[0])
                else:
                    db.execute(
                        """INSERT INTO tasks
                           (task_id, request_id, request_sha256, kind,
                            wind_farm_id, owner_id, state, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?)""",
                        (
                            task_id, request_id, request_sha256, kind,
                            wind_farm_id, owner_id, datetime.now(UTC).isoformat(),
                        ),
                    )
                    result = task_id
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return result


    def request_partition_retry(
        self, *, request_id: str, topic: str, partition: int, expected_offset: int
    ) -> bool:
        """Transactionally unlock only the exact blocked offset, idempotently."""
        if not request_id or not topic or partition < 0 or expected_offset < 0:
            raise ValueError("invalid partition retry request")
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                previous = db.execute(
                    """SELECT topic, partition_id, offset_id
                       FROM retry_requests WHERE request_id=?""",
                    (request_id,),
                ).fetchone()
                requested = (topic, partition, expected_offset)
                if previous is not None:
                    if tuple(previous) != requested:
                        raise MetadataConflict("retry request ID reused with different arguments")
                    db.execute("COMMIT")
                    return True
                current = db.execute(
                    """SELECT offset_id FROM blocked_partitions
                       WHERE topic=? AND partition_id=?""",
                    (topic, partition),
                ).fetchone()
                if current is None or current[0] != expected_offset:
                    db.execute("COMMIT")
                    return False
                db.execute(
                    """INSERT INTO retry_requests
                       (request_id, topic, partition_id, offset_id) VALUES (?,?,?,?)""",
                    (request_id, topic, partition, expected_offset),
                )
                db.execute(
                    """DELETE FROM blocked_partitions
                       WHERE topic=? AND partition_id=? AND offset_id=?""",
                    (topic, partition, expected_offset),
                )
                db.execute("COMMIT")
                return True
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def list_blocked(self, *, page_size: int, after: tuple[str, int] | None = None
    ) -> list[tuple[str, int, int, str]]:
        """List blocked partitions in deterministic order."""
        if not 1 <= page_size <= 1001:
            raise ValueError("invalid page size")
        cursor = after or ("", -1)
        with self._mutex:
            rows = self._db().execute(
                """SELECT topic, partition_id, offset_id, reason
                   FROM blocked_partitions
                   WHERE topic > ? OR (topic = ? AND partition_id > ?)
                   ORDER BY topic, partition_id LIMIT ?""",
                (cursor[0], cursor[0], cursor[1], page_size),
            ).fetchall()
            return [(str(a), int(b), int(c), str(d)) for a, b, c, d in rows]

    def list_tables(
        self, *, database: str | None = None
    ) -> list[ObjectRegistration]:
        """Read durable identities; queries must never discover arbitrary unmanaged tables."""
        with self._mutex:
            rows = self._db().execute(
                """SELECT name, full_sha256, identity_json, schema_fingerprint, state
                   FROM storage_objects WHERE name LIKE 'tb_%' AND state='READY'
                   ORDER BY name"""
            ).fetchall()
        records = [
            ObjectRegistration(str(row[0]), str(row[1]), str(row[2]), str(row[3]), row[4])
            for row in rows
        ]
        if database is None:
            return records
        return [
            record for record in records
            if record.name.startswith("tb_") and isinstance(record.identity_json, str)
            and hashlib.sha256(
                json.dumps([json.loads(record.identity_json)[0]],
                           ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()[:24] == database.removeprefix("wh_")
        ]

    def get_task(
        self, task_id: str, *, kind: str, owner_id: str
    ) -> TaskRecord | None:
        if not task_id or kind not in {"EXPORT", "BACKUP"}:
            raise ValueError("invalid task query")
        with self._mutex:
            row = self._db().execute(
                """SELECT task_id, kind, wind_farm_id, owner_id,
                          state, created_at, finished_at,
                          artifact_id, size_bytes, sha256, error_code, message
                   FROM tasks WHERE task_id=? AND kind=? AND owner_id=?""",
                (task_id, kind, owner_id),
            ).fetchone()
            return TaskRecord(*row) if row else None

    def set_task_state(
        self, task_id: str, *, state: str, artifact_id: str = "",
        size_bytes: int = 0, sha256: str = "", error_code: str = "",
        message: str = "",
    ) -> None:
        """Persistent state transition; terminal jobs cannot be reopened."""
        valid = {
            "QUEUED": {"RUNNING", "FAILED", "INTERRUPTED"},
            "RUNNING": {"SUCCEEDED", "FAILED", "INTERRUPTED"},
        }
        if state not in {"RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"}:
            raise ValueError("invalid task state transition")
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT state FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None or state not in valid.get(row[0], set()):
                    raise MetadataConflict("task state transition is not allowed")
                terminal = state != "RUNNING"
                db.execute(
                    """UPDATE tasks
                       SET state=?, artifact_id=?, size_bytes=?, sha256=?,
                           error_code=?, message=?, finished_at=?
                       WHERE task_id=?""",
                    (
                        state, artifact_id, size_bytes, sha256, error_code,
                        message[:1024], datetime.now(UTC).isoformat() if terminal else None,
                        task_id,
                    ),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def interrupt_unfinished(self) -> int:
        """On crash recovery, durable QUEUED/RUNNING jobs must not remain stuck."""
        with self._mutex:
            result = self._db().execute(
                """UPDATE tasks SET state='INTERRUPTED',
                    finished_at=?, error_code='PROCESS_RESTART'
                   WHERE state IN ('QUEUED', 'RUNNING')""",
                (datetime.now(UTC).isoformat(),),
            )
            return int(result.rowcount)

    def close(self) -> None:
        with self._mutex:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            fd = getattr(self, "_lock_file", None)
            if fd is not None:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                fd.close()
                self._lock_file = None
