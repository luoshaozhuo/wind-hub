"""SQLite command event journal: atomic acceptance, outbox and crash recovery."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import uuid4

from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
    TERMINAL_STATES,
    utc_now,
    validate_transition,
)


class RepositoryError(RuntimeError):
    """Persistence unavailable; never perform a field write after this failure."""


class StorageFullError(RepositoryError):
    """Admission must stop to preserve terminal-event capacity."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_events (
    event_seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    command_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('ACCEPTED','CONNECTING','EXECUTING','COMPLETED')
    ),
    occurred_at TEXT NOT NULL,
    request_id TEXT,
    caller_id TEXT,
    operator_id TEXT,
    trace_id TEXT,
    device_id TEXT,
    point_id TEXT,
    requested_value BLOB,
    config_reference TEXT,
    receive_seq INTEGER,
    state TEXT NOT NULL,
    outcome TEXT,
    error_code TEXT,
    error_message TEXT,
    duration_ms INTEGER,
    detail_json TEXT,
    publish_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (publish_state IN ('PENDING','PUBLISHED')),
    published_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    CHECK (event_type <> 'ACCEPTED' OR (
        request_id IS NOT NULL AND caller_id IS NOT NULL
        AND device_id IS NOT NULL AND point_id IS NOT NULL
        AND requested_value IS NOT NULL AND receive_seq IS NOT NULL
    ))
);
CREATE UNIQUE INDEX IF NOT EXISTS ce_accept_once
    ON command_events(command_id) WHERE event_type='ACCEPTED';
CREATE UNIQUE INDEX IF NOT EXISTS ce_terminal_once
    ON command_events(command_id) WHERE event_type='COMPLETED';
CREATE INDEX IF NOT EXISTS ce_command_seq ON command_events(command_id,event_seq DESC);
CREATE INDEX IF NOT EXISTS ce_request_query
    ON command_events(request_id,event_seq) WHERE event_type='ACCEPTED';
CREATE INDEX IF NOT EXISTS ce_pending_publish
    ON command_events(event_seq) WHERE publish_state='PENDING';
CREATE INDEX IF NOT EXISTS ce_device_order
    ON command_events(device_id,receive_seq) WHERE event_type='ACCEPTED';
CREATE TABLE IF NOT EXISTS repository_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
INSERT OR IGNORE INTO repository_meta(key,value) VALUES ('schema_version','1');
"""


_T = TypeVar("_T")


class EventRepository:
    """Single process writer; connection must not be shared with another process."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_storage_bytes: int = 64 * 1024**3,
        reserved_storage_bytes: int = 6871947674,
        admission_stop_ratio: float = 0.90,
        retention_seconds: int = 1800,
    ) -> None:
        if (
            max_storage_bytes <= 0
            or not 0 < reserved_storage_bytes < max_storage_bytes
            or not 0 < admission_stop_ratio <= 1
            or retention_seconds < 0
        ):
            raise ValueError("invalid storage limits")
        self._path = Path(path)
        self._max_bytes = max_storage_bytes
        self._reserve = reserved_storage_bytes
        self._stop_ratio = admission_stop_ratio
        self._retention = retention_seconds
        self._closed = False
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._path.with_suffix(self._path.suffix + '.lock').open('a+b')
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            raise RepositoryError('Commander database is owned by another process') from exc
        try:
            self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        except BaseException:
            self._lock_file.close()
            raise
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)
            row = self._db.execute(
                "SELECT value FROM repository_meta WHERE key='schema_version'"
            ).fetchone()
            if row != ("1",):
                raise RepositoryError("unsupported repository schema")
        except BaseException:
            self._db.close()
            self._lock_file.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def _occupied(self) -> int:
        return sum(
            p.stat().st_size
            for p in (
                self._path,
                Path(f"{self._path}-wal"),
                Path(f"{self._path}-shm"),
            )
            if p.exists()
        )

    def _admit(self) -> None:
        available = min(
            self._max_bytes * self._stop_ratio,
            self._max_bytes - self._reserve,
        )
        if self._occupied() >= available:
            raise StorageFullError("SQLite capacity reserved for completion and recovery")
        stat = os.statvfs(self._path.parent)
        if stat.f_bavail * stat.f_frsize < min(self._reserve, self._max_bytes):
            raise StorageFullError("insufficient filesystem space for command completion")

    def _transaction(self, operation: Callable[[], _T]) -> _T:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                result = operation()
                self._db.execute("COMMIT")
                return result
            except BaseException:
                if self._db.in_transaction:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error as exc:
                        raise RepositoryError("rollback failed; repository unusable") from exc
                raise

    def accept(
        self,
        specs: Sequence[CommandSpec],
        *,
        caller_id: str,
        operator_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[AcceptedCommand, ...]:
        if not specs or not caller_id.strip():
            raise ValueError("nonempty batch and caller_id required")
        # Serialize before the transaction so unsupported objects reject the entire batch.
        encoded = [
            json.dumps(spec.value, allow_nan=False, separators=(",", ":")).encode("utf-8")
            for spec in specs
        ]
        request_id = str(uuid4())
        now = utc_now()

        def insert() -> tuple[AcceptedCommand, ...]:
            self._admit()
            if self._occupied() + sum(len(value) for value in encoded) + len(specs) * 4096 >= min(
                self._max_bytes * self._stop_ratio, self._max_bytes - self._reserve
            ):
                raise StorageFullError("batch exceeds admission capacity")
            row = self._db.execute(
                "SELECT value FROM repository_meta WHERE key='receive_seq'"
            ).fetchone()
            seq = int(row[0]) if row else 0
            accepted: list[AcceptedCommand] = []
            for spec, value in zip(specs, encoded, strict=True):
                seq += 1
                command_id = str(uuid4())
                self._db.execute(
                    """INSERT INTO command_events (
                        event_id,command_id,event_type,occurred_at,request_id,
                        caller_id,operator_id,trace_id,device_id,point_id,
                        requested_value,config_reference,receive_seq,state,detail_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()), command_id, "ACCEPTED", now.isoformat(),
                        request_id, caller_id, operator_id, trace_id, spec.device_id,
                        spec.point_id, value, spec.config_reference, seq,
                        CommandState.ACCEPTED.value,
                        json.dumps(
                            {"expires_at": spec.expires_at.isoformat() if spec.expires_at else None}
                        ),
                    ),
                )
                accepted.append(
                    AcceptedCommand(command_id, request_id, caller_id, spec, now, seq)
                )
            self._db.execute(
                "INSERT INTO repository_meta(key,value) VALUES('receive_seq',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(seq),),
            )
            return tuple(accepted)

        return self._transaction(insert)

    def _current_state(self, command_id: str) -> CommandState:
        row = self._db.execute(
            "SELECT state FROM command_events WHERE command_id=? "
            "ORDER BY event_seq DESC LIMIT 1",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return CommandState(row[0])

    def transition(
        self,
        command_id: str,
        state: CommandState,
        *,
        outcome: CommandOutcome | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        def insert() -> None:
            previous = self._current_state(command_id)
            validate_transition(previous, state, outcome)
            event_type = "COMPLETED" if state in TERMINAL_STATES else state.value
            self._db.execute(
                """INSERT INTO command_events(
                    event_id,command_id,event_type,occurred_at,state,outcome,
                    error_code,error_message,publish_state
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), command_id, event_type, utc_now().isoformat(),
                    state.value, outcome.value if outcome is not None else None,
                    error_code, error_message,
                    "PENDING" if event_type == "COMPLETED" else "PUBLISHED",
                ),
            )

        self._transaction(insert)

    def get(self, command_id: str) -> CommandResult | None:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_type,occurred_at,request_id,caller_id,device_id,point_id,"
                "requested_value,config_reference,receive_seq,state,outcome,"
                "error_code,error_message,detail_json FROM command_events "
                "WHERE command_id=? ORDER BY event_seq",
                (command_id,),
            ).fetchall()
        if not rows:
            return None
        first = rows[0]
        if first[0] != "ACCEPTED":
            raise RepositoryError("command has no accepted event")
        detail = json.loads(first[13] or "{}")
        expires = detail.get("expires_at")
        spec = CommandSpec(
            device_id=first[4], point_id=first[5],
            value=json.loads(first[6]), config_reference=first[7],
            expires_at=datetime.fromisoformat(expires) if expires else None,
        )
        command = AcceptedCommand(
            command_id, first[2], first[3], spec,
            datetime.fromisoformat(first[1]), first[8],
        )
        last = rows[-1]
        state = CommandState(last[9])
        return CommandResult(
            command, state, CommandOutcome(last[10]) if last[10] else None,
            last[11], last[12],
            datetime.fromisoformat(last[1]) if state in TERMINAL_STATES else None,
        )

    def get_request(self, request_id: str) -> tuple[CommandResult, ...]:
        """Return every accepted child in durable receive order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT command_id FROM command_events WHERE request_id=? "
                "AND event_type='ACCEPTED' ORDER BY receive_seq",
                (request_id,),
            ).fetchall()
        results = (self.get(command_id) for (command_id,) in rows)
        return tuple(result for result in results if result is not None)

    def pending_events(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            self._db.row_factory = sqlite3.Row
            try:
                rows = self._db.execute(
                    "SELECT * FROM command_events WHERE publish_state='PENDING' "
                    "AND event_type IN ('ACCEPTED','COMPLETED') "
                    "ORDER BY event_seq LIMIT ?",
                    (limit,),
                ).fetchall()
                return tuple(dict(row) for row in rows)
            finally:
                self._db.row_factory = None

    def mark_published(self, event_id: str) -> None:
        def update() -> None:
            self._db.execute(
                "UPDATE command_events SET publish_state='PUBLISHED',"
                "published_at=? WHERE event_id=? AND publish_state='PENDING'",
                (utc_now().isoformat(), event_id),
            )

        self._transaction(update)

    def record_publish_failure(self, event_id: str) -> None:
        self._transaction(
            lambda: self._db.execute(
                "UPDATE command_events SET attempts=attempts+1 WHERE event_id=?",
                (event_id,),
            )
        )

    def recover(self) -> tuple[str, ...]:
        """Finalize unfinished commands; no field I/O is ever performed."""
        def operation() -> tuple[str, ...]:
            rows = self._db.execute(
                "SELECT command_id FROM command_events WHERE event_type='ACCEPTED' "
                "AND command_id NOT IN "
                "(SELECT command_id FROM command_events WHERE event_type='COMPLETED') "
                "ORDER BY receive_seq"
            ).fetchall()
            recovered: list[str] = []
            for (command_id,) in rows:
                previous = self._current_state(command_id)
                if previous == CommandState.EXECUTING:
                    state = CommandState.UNKNOWN
                    outcome = CommandOutcome.UNKNOWN
                else:
                    state = CommandState.INTERRUPTED
                    outcome = CommandOutcome.NOT_SENT
                validate_transition(previous, state, outcome)
                self._db.execute(
                    "INSERT INTO command_events("
                    "event_id,command_id,event_type,occurred_at,state,outcome)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        command_id,
                        "COMPLETED",
                        utc_now().isoformat(),
                        state.value,
                        outcome.value,
                    ),
                )
                recovered.append(command_id)
            return tuple(recovered)

        return self._transaction(operation)

    def cleanup(self, limit: int = 100) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff = (utc_now() - timedelta(seconds=self._retention)).isoformat()

        def purge() -> int:
            rows = self._db.execute(
                """SELECT a.request_id
                   FROM command_events AS a
                   WHERE a.event_type='ACCEPTED'
                   GROUP BY a.request_id
                   HAVING COUNT(*) = SUM(
                       CASE WHEN EXISTS (
                           SELECT 1 FROM command_events AS t
                           WHERE t.command_id=a.command_id
                             AND t.event_type='COMPLETED'
                             AND t.occurred_at<=?
                       ) AND NOT EXISTS (
                           SELECT 1 FROM command_events AS e
                           WHERE e.command_id=a.command_id
                             AND e.publish_state='PENDING'
                       ) THEN 1 ELSE 0 END
                   )
                   AND MAX(a.occurred_at)<=?
                   ORDER BY MIN(a.event_seq) LIMIT ?""",
                (cutoff, cutoff, limit),
            ).fetchall()
            deleted = 0
            for (request_id,) in rows:
                command_rows = self._db.execute(
                    "SELECT command_id FROM command_events "
                    "WHERE request_id=? AND event_type='ACCEPTED'",
                    (request_id,),
                ).fetchall()
                for (command_id,) in command_rows:
                    self._db.execute(
                        "DELETE FROM command_events WHERE command_id=?",
                        (command_id,),
                    )
                    deleted += 1
            return deleted

        return self._transaction(purge)
