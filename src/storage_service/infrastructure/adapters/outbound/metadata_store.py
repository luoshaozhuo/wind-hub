"""Crash-recoverable single-owner SQLite metadata and idempotent tasks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Self

from storage_service.application.config import DeviceBinding, DeviceModel
from storage_service.domain.errors import WideTableNotCreatedError
from storage_service.domain.tasks import TaskRecord


@dataclass(frozen=True, slots=True)
class ObjectRegistration:
    """Physical name and full identity must never be silently rebound."""

    name: str
    full_sha256: str
    identity_json: str
    schema_fingerprint: str
    state: Literal["CREATING", "READY"]


class MetadataConflictError(RuntimeError):
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
        if self._path.is_symlink() or self._path.parent.is_symlink():
            raise ValueError("Storage metadata path must not use symbolic links")
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        fd_number = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fd = os.fdopen(fd_number, "a+b")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            connection = sqlite3.connect(
                self._path, isolation_level=None, timeout=10.0, check_same_thread=False
            )
            os.chmod(self._path, 0o600)
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
        if version is None or version[0] not in (0, 1, 2):
            raise MetadataConflictError("unsupported SQLite metadata schema")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_objects (
                name TEXT PRIMARY KEY,
                full_sha256 TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                logical_identity_hash TEXT NOT NULL,
                physical_database TEXT NOT NULL,
                stable_name TEXT NOT NULL,
                table_name TEXT NOT NULL,
                closure_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS semantic_pins (
                point_table_id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                business_config_id TEXT NOT NULL DEFAULT '',
                business_version_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS device_binding_pins (
                wind_farm_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                point_table_id TEXT NOT NULL,
                device_version_id TEXT NOT NULL,
                binding_json TEXT NOT NULL,
                PRIMARY KEY (
                    wind_farm_id, device_id, point_table_id, device_version_id
                )
            );
            CREATE TABLE IF NOT EXISTS retry_requests (
                request_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                partition_id INTEGER NOT NULL,
                offset_id INTEGER NOT NULL
            );
            PRAGMA user_version=2;
            """
        )
        expected = {
            "name",
            "full_sha256",
            "identity_json",
            "schema_fingerprint",
            "logical_identity_hash",
            "physical_database",
            "stable_name",
            "table_name",
            "closure_hash",
            "created_at",
            "state",
        }
        observed = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(storage_objects)").fetchall()
        }
        if observed != expected:
            raise MetadataConflictError(
                "existing Storage metadata schema requires an explicit migration"
            )

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("metadata store is not open")
        return self._connection

    def reserve_object(
        self,
        name: str,
        full_sha256: str,
        identity_json: str,
        schema_fingerprint: str,
        *,
        physical_database: str,
        stable_name: str,
        table_name: str,
        closure_hash: str,
    ) -> ObjectRegistration:
        """Reserve full logical/physical identity before issuing any TDengine DDL."""
        details = (
            full_sha256,
            identity_json,
            schema_fingerprint,
            full_sha256,
            physical_database,
            stable_name,
            table_name,
            closure_hash,
        )
        if any(
            not isinstance(value, str) or not value
            for value in (
                name,
                full_sha256,
                identity_json,
                schema_fingerprint,
                physical_database,
                closure_hash,
            )
        ):
            raise ValueError("physical object metadata is incomplete")
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                record = db.execute(
                    """SELECT full_sha256, identity_json, schema_fingerprint,
                              logical_identity_hash, physical_database,
                              stable_name, table_name, closure_hash, state
                       FROM storage_objects WHERE name = ?""",
                    (name,),
                ).fetchone()
                if record is None:
                    db.execute(
                        """INSERT INTO storage_objects
                           (name, full_sha256, identity_json, schema_fingerprint,
                            logical_identity_hash, physical_database,
                            stable_name, table_name, closure_hash, created_at, state)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATING')""",
                        (name, *details, datetime.now(UTC).isoformat()),
                    )
                    state: Literal["CREATING", "READY"] = "CREATING"
                else:
                    if tuple(record[:8]) != details:
                        raise MetadataConflictError(f"physical object identity collision: {name}")
                    state = record[8]
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return ObjectRegistration(name, full_sha256, identity_json, schema_fingerprint, state)

    def require_ready_wide_table(
        self, *, name: str, fingerprint: str, identity_sha256: str
    ) -> None:
        """Require durable exact physical ownership before any historical read."""
        with self._mutex:
            row = (
                self._db()
                .execute(
                    """SELECT full_sha256, schema_fingerprint, state
                   FROM storage_objects WHERE name = ?""",
                    (name,),
                )
                .fetchone()
            )
        if row is None:
            raise WideTableNotCreatedError("historical wide table was never registered")
        if row != (identity_sha256, fingerprint, "READY"):
            raise MetadataConflictError(
                "wide historical table is not READY under the requested immutable identity"
            )

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
                    registration.name,
                    registration.full_sha256,
                    registration.identity_json,
                    registration.schema_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise MetadataConflictError("reservation changed or disappeared")

    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        """Retain the original poison record; never replace an earlier blocker."""
        with self._mutex:
            import hashlib

            if not topic or partition < 0 or offset < 0 or len(payload) > 16 * 1024 * 1024:
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
                    raise MetadataConflictError("cannot overwrite an earlier blocked Kafka record")
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
            row = (
                self._db()
                .execute(
                    """SELECT offset_id, reason FROM blocked_partitions
                   WHERE topic = ? AND partition_id = ?""",
                    (topic, partition),
                )
                .fetchone()
            )
            return (int(row[0]), str(row[1])) if row is not None else None

    def clear_blocked_partition(self, topic: str, partition: int, *, expected_offset: int) -> bool:
        with self._mutex:
            cursor = self._db().execute(
                """DELETE FROM blocked_partitions
                   WHERE topic = ? AND partition_id = ? AND offset_id = ?""",
                (topic, partition, expected_offset),
            )
            return cursor.rowcount == 1

    def register_task(
        self,
        *,
        task_id: str,
        request_id: str,
        request_sha256: str,
        kind: str,
        wind_farm_id: str,
        owner_id: str,
    ) -> str:
        """Create idempotently: same request ID with different content is fatal."""
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    """SELECT task_id, request_sha256, kind, wind_farm_id, owner_id
                       FROM tasks WHERE request_id = ?""",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    if row[1:] != (request_sha256, kind, wind_farm_id, owner_id):
                        raise MetadataConflictError("request_id reused with different parameters")
                    result = str(row[0])
                else:
                    db.execute(
                        """INSERT INTO tasks
                           (task_id, request_id, request_sha256, kind,
                            wind_farm_id, owner_id, state, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?)""",
                        (
                            task_id,
                            request_id,
                            request_sha256,
                            kind,
                            wind_farm_id,
                            owner_id,
                            datetime.now(UTC).isoformat(),
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
                        raise MetadataConflictError(
                            "retry request ID reused with different arguments"
                        )
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

    def list_blocked(
        self, *, page_size: int, after: tuple[str, int] | None = None
    ) -> list[tuple[str, int, int, str]]:
        """List blocked partitions in deterministic order."""
        if not 1 <= page_size <= 1001:
            raise ValueError("invalid page size")
        cursor = after or ("", -1)
        with self._mutex:
            rows = (
                self._db()
                .execute(
                    """SELECT topic, partition_id, offset_id, reason
                   FROM blocked_partitions
                   WHERE topic > ? OR (topic = ? AND partition_id > ?)
                   ORDER BY topic, partition_id LIMIT ?""",
                    (cursor[0], cursor[0], cursor[1], page_size),
                )
                .fetchall()
            )
            return [(str(a), int(b), int(c), str(d)) for a, b, c, d in rows]

    def list_ready_wide_tables(self, *, database: str) -> tuple[ObjectRegistration, ...]:
        """Return only exact-hash-verified wide subtable ownership for one farm."""
        if (
            not isinstance(database, str)
            or re.fullmatch(r"wh_[a-z0-9_]+_[0-9a-f]{16}", database) is None
        ):
            raise ValueError("invalid wide database identity")
        with self._mutex:
            rows = (
                self._db()
                .execute(
                    """SELECT name, full_sha256, identity_json, schema_fingerprint, state,
                          physical_database, table_name
                   FROM storage_objects
                   WHERE physical_database = ? AND table_name <> ''
                         AND state = 'READY' ORDER BY name""",
                    (database,),
                )
                .fetchall()
            )
        result: list[ObjectRegistration] = []
        for name, digest, identity_json, schema, state, db_name, table_name in rows:
            if (
                not isinstance(name, str)
                or not isinstance(digest, str)
                or not isinstance(identity_json, str)
                or hashlib.sha256(identity_json.encode("utf-8")).hexdigest() != digest
                or not name == f"{database}.{table_name}"
                or db_name != database
                or re.fullmatch(r"tb_[a-z0-9_]+_[0-9a-f]{16}", table_name) is None
            ):
                raise MetadataConflictError("wide subtable registry ownership is corrupt")
            try:
                parts = json.loads(identity_json)
            except (TypeError, ValueError) as exc:
                raise MetadataConflictError("wide subtable identity is malformed") from exc
            if (
                not isinstance(parts, list)
                or len(parts) != 6
                or any(not isinstance(item, str) or not item for item in parts)
            ):
                raise MetadataConflictError("wide subtable full identity is incomplete")
            result.append(ObjectRegistration(name, digest, identity_json, schema, state))
        return tuple(result)

    def list_tables(
        self,
        *,
        database: str | None = None,
    ) -> list[ObjectRegistration]:
        """Return only integrity-checked registered and READY physical objects."""
        with self._mutex:
            rows = (
                self._db()
                .execute(
                    """SELECT name, full_sha256, identity_json,
                          schema_fingerprint, state
                   FROM storage_objects
                   WHERE name LIKE 'tb_%' AND state='READY' ORDER BY name"""
                )
                .fetchall()
            )
        records: list[ObjectRegistration] = []
        for name, digest, canonical, fingerprint, state in rows:
            if (
                not isinstance(name, str)
                or re.fullmatch(r"tb_[0-9a-f]{24}", name) is None
                or not isinstance(canonical, str)
                or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest
            ):
                raise MetadataConflictError("stored physical identity is not trustworthy")
            try:
                identity = json.loads(canonical)
            except (ValueError, TypeError) as exc:
                raise MetadataConflictError("stored physical identity is malformed") from exc
            if (
                not isinstance(identity, list)
                or len(identity) != 6
                or any(not isinstance(item, str) or not item for item in identity)
            ):
                raise MetadataConflictError("stored table identity has invalid components")
            if name != f"tb_{digest[:24]}":
                raise MetadataConflictError("physical table name differs from identity hash")
            farm_hash = hashlib.sha256(
                json.dumps([identity[0]], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if database is not None and database != f"wh_{farm_hash[:24]}":
                continue
            records.append(
                ObjectRegistration(
                    name,
                    digest,
                    canonical,
                    fingerprint,
                    state,
                )
            )
        return records

    def register_semantic_pins(
        self,
        *,
        point_table_versions: dict[str, tuple[str, str]],
        business_points_pin: tuple[str, str] | None,
        device_bindings: Iterable[DeviceBinding] = (),
    ) -> None:
        """Never rebind a historical sample identity to another YAML closure."""
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                for full_id, (config_id, version_id) in point_table_versions.items():
                    business_id, business_version = business_points_pin or ("", "")
                    identity = (
                        config_id,
                        version_id,
                        business_id,
                        business_version,
                    )
                    prior = db.execute(
                        """SELECT config_id, version_id,
                                  business_config_id, business_version_id
                           FROM semantic_pins WHERE point_table_id=?""",
                        (full_id,),
                    ).fetchone()
                    if prior is not None and tuple(prior) != identity:
                        raise MetadataConflictError(
                            "immutable PointTable semantic closure cannot be rebound"
                        )
                    if prior is None:
                        db.execute(
                            """INSERT INTO semantic_pins
                               (point_table_id, config_id, version_id,
                                business_config_id, business_version_id)
                               VALUES (?, ?, ?, ?, ?)""",
                            (full_id, *identity),
                        )
                for binding in device_bindings:
                    table_pin = point_table_versions.get(binding.point_table_id)
                    if table_pin is None:
                        existing_pin = db.execute(
                            """SELECT config_id, version_id FROM semantic_pins
                               WHERE point_table_id=?""",
                            (binding.point_table_id,),
                        ).fetchone()
                        if existing_pin is None:
                            raise MetadataConflictError(
                                "device binding has no durable PointTable identity"
                            )
                    identity = (
                        binding.wind_farm_id,
                        binding.device_id,
                        binding.point_table_id,
                        binding.device_version_id,
                    )
                    raw = json.dumps(
                        asdict(binding),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    previous = db.execute(
                        """SELECT binding_json FROM device_binding_pins
                           WHERE wind_farm_id=? AND device_id=?
                             AND point_table_id=? AND device_version_id=?""",
                        identity,
                    ).fetchone()
                    if previous is not None and previous[0] != raw:
                        raise MetadataConflictError("immutable device binding cannot be rebound")
                    if previous is None:
                        db.execute(
                            """INSERT INTO device_binding_pins (
                               wind_farm_id, device_id, point_table_id,
                               device_version_id, binding_json
                               ) VALUES (?, ?, ?, ?, ?)""",
                            (*identity, raw),
                        )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def list_device_bindings(self) -> tuple[DeviceBinding, ...]:
        """Recover all pinned historical devices without selecting latest models."""
        with self._mutex:
            rows = (
                self._db()
                .execute(
                    """SELECT binding_json FROM device_binding_pins
                   ORDER BY wind_farm_id, device_id, point_table_id,
                            device_version_id"""
                )
                .fetchall()
            )
        result: list[DeviceBinding] = []
        for (serialized,) in rows:
            try:
                payload = json.loads(serialized)
                model = DeviceModel(**payload.pop("model"))
                result.append(DeviceBinding(**payload, model=model))
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise MetadataConflictError(
                    "corrupted immutable historical device binding"
                ) from exc
        return tuple(result)

    def list_semantic_pins(
        self,
    ) -> dict[str, tuple[tuple[str, str], tuple[str, str] | None]]:
        with self._mutex:
            rows = (
                self._db()
                .execute(
                    """SELECT point_table_id, config_id, version_id,
                          business_config_id, business_version_id
                   FROM semantic_pins ORDER BY point_table_id"""
                )
                .fetchall()
            )
        return {
            str(full_id): (
                (str(config_id), str(version_id)),
                (str(business_id), str(business_version))
                if business_id and business_version
                else None,
            )
            for full_id, config_id, version_id, business_id, business_version in rows
        }

    def get_task(self, task_id: str, *, kind: str, owner_id: str) -> TaskRecord | None:
        if not task_id or kind not in {"EXPORT", "BACKUP"}:
            raise ValueError("invalid task query")
        with self._mutex:
            row = (
                self._db()
                .execute(
                    """SELECT task_id, kind, wind_farm_id, owner_id,
                          state, created_at, finished_at,
                          artifact_id, size_bytes, sha256, error_code, message
                   FROM tasks WHERE task_id=? AND kind=? AND owner_id=?""",
                    (task_id, kind, owner_id),
                )
                .fetchone()
            )
            return TaskRecord(*row) if row else None

    def set_task_state(
        self,
        task_id: str,
        *,
        state: str,
        artifact_id: str = "",
        size_bytes: int = 0,
        sha256: str = "",
        error_code: str = "",
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
                row = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None or state not in valid.get(row[0], set()):
                    raise MetadataConflictError("task state transition is not allowed")
                terminal = state != "RUNNING"
                db.execute(
                    """UPDATE tasks
                       SET state=?, artifact_id=?, size_bytes=?, sha256=?,
                           error_code=?, message=?, finished_at=?
                       WHERE task_id=?""",
                    (
                        state,
                        artifact_id,
                        size_bytes,
                        sha256,
                        error_code,
                        message[:1024],
                        datetime.now(UTC).isoformat() if terminal else None,
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
