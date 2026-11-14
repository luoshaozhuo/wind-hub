"""SQLite command event history: write-behind snapshots and audit outbox."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from commander.application.journal_port import JournalMetrics
from commander.domain.command import (
    TERMINAL_STATES,
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
    utc_now,
    validate_transition,
)


class RepositoryError(RuntimeError):
    """Persistence unavailable; new control admissions fail closed."""


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
CREATE UNIQUE INDEX IF NOT EXISTS ce_request_once
    ON command_events(caller_id,request_id)
    WHERE event_type='ACCEPTED'
      AND json_extract(detail_json, '$.request_index')=0;
CREATE INDEX IF NOT EXISTS ce_command_seq ON command_events(command_id,event_seq DESC);
CREATE INDEX IF NOT EXISTS ce_request_query
    ON command_events(request_id,event_seq) WHERE event_type='ACCEPTED';
CREATE INDEX IF NOT EXISTS ce_pending_publish
    ON command_events(event_seq) WHERE publish_state='PENDING';
CREATE INDEX IF NOT EXISTS ce_device_order
    ON command_events(device_id,receive_seq) WHERE event_type='ACCEPTED';
CREATE TABLE IF NOT EXISTS recovery_decisions (
    decision_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    token_id TEXT NOT NULL UNIQUE,
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch > 0),
    old_instance TEXT NOT NULL,
    new_instance TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    publish_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (publish_state IN ('PENDING','PUBLISHED')),
    published_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(caller_id, request_id)
);
CREATE INDEX IF NOT EXISTS recovery_pending_publish
    ON recovery_decisions(occurred_at) WHERE publish_state='PENDING';
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
        retention_seconds: int = 1800,
    ) -> None:
        if max_storage_bytes <= 0 or retention_seconds < 0:
            raise ValueError("invalid storage limits")
        self._path = Path(path)
        self._max_bytes = max_storage_bytes
        self._retention = retention_seconds
        self._closed = False
        self._unavailable = False
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._path.with_suffix(self._path.suffix + ".lock").open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            raise RepositoryError("Commander database is owned by another process") from exc
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

    @property
    def operational(self) -> bool:
        return not self._closed and not self._unavailable

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def metrics(self) -> JournalMetrics:
        with self._lock:
            if not self.operational:
                raise RepositoryError("command journal is unavailable")
            used = self._occupied()
            accepted = self._db.execute(
                "SELECT COUNT(*) FROM command_events WHERE event_type='ACCEPTED'"
            ).fetchone()[0]
            pending, oldest = self._db.execute(
                "SELECT COUNT(*), MIN(occurred_at) FROM command_events "
                "WHERE publish_state='PENDING'"
            ).fetchone()
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM command_events "
                "WHERE event_type='COMPLETED' GROUP BY state"
            ).fetchall()
            age_ms = 0.0
            if oldest is not None:
                age_ms = max(
                    0.0,
                    (utc_now() - datetime.fromisoformat(oldest)).total_seconds() * 1000,
                )
            return JournalMetrics(
                used_bytes=used,
                accepted_total=accepted,
                pending_events=pending,
                oldest_pending_age_ms=age_ms,
                terminal_counts=tuple((str(name), int(count)) for name, count in rows),
            )

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

    def _allow_write_behind_flush(self) -> None:
        """Flush already accepted events using the completion-space reserve.

        The admission stop ratio applies to NEW controls, not to events already
        accepted into bounded memory. Otherwise the outbox can deadlock.
        """
        if self._occupied() >= self._max_bytes:
            raise StorageFullError("SQLite hard capacity exceeded")
        filesystem = os.statvfs(self._path.parent)
        if filesystem.f_bavail * filesystem.f_frsize < 4096:
            raise StorageFullError("filesystem has no space for outstanding command events")

    def _transaction(self, operation: Callable[[], _T]) -> _T:
        with self._lock:
            if self._closed or self._unavailable:
                raise RepositoryError("command journal is unavailable")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                result = operation()
                self._db.execute("COMMIT")
                return result
            except BaseException as exc:
                if self._db.in_transaction:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error as rollback_error:
                        self._unavailable = True
                        raise RepositoryError(
                            "rollback failed; repository unusable"
                        ) from rollback_error
                if isinstance(exc, sqlite3.Error):
                    # A successful rollback preserves the SQLite connection for
                    # bounded write-behind retry after transient disk failure.
                    raise RepositoryError("SQLite transaction failed; retryable") from exc
                raise

    def recovery_blocked_devices(self) -> frozenset[str]:
        """Restore uncertain field control safety across repeated process crashes."""
        with self._lock:
            if not self.operational:
                raise RepositoryError("command journal is unavailable")
            rows = self._db.execute(
                "SELECT DISTINCT a.device_id FROM command_events AS a "
                "JOIN command_events AS t ON t.command_id=a.command_id "
                "AND t.event_type='COMPLETED' AND t.state='UNKNOWN' "
                "WHERE a.event_type='ACCEPTED' AND NOT EXISTS ("
                "SELECT 1 FROM recovery_decisions AS r "
                "WHERE r.device_id=a.device_id AND r.occurred_at>=t.occurred_at)"
            ).fetchall()
            return frozenset(str(row[0]) for row in rows)

    def latest_receive_seq(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM repository_meta WHERE key='receive_seq'"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def append_events(self, events: Sequence[dict[str, Any]]) -> None:
        """Persist ordered volatile events, idempotently across worker retries."""
        if not events:
            return
        columns = (
            "event_id",
            "command_id",
            "event_type",
            "occurred_at",
            "request_id",
            "caller_id",
            "operator_id",
            "trace_id",
            "device_id",
            "point_id",
            "requested_value",
            "config_reference",
            "receive_seq",
            "state",
            "outcome",
            "error_code",
            "error_message",
            "detail_json",
        )
        placeholders = ",".join("?" for _ in columns)
        # ON CONFLICT DO NOTHING 仅吞掉唯一约束冲突（worker 重试/乱序批次的
        # 幂等重放）；NOT NULL/CHECK 违约仍须抛出并整批回滚，避免静默丢事件。
        statement = (
            f"INSERT INTO command_events ({','.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        )

        def write() -> None:
            self._allow_write_behind_flush()
            accepted: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for event in events:
                if event.get("event_type") == "ACCEPTED":
                    key = (str(event["caller_id"]), str(event["request_id"]))
                    accepted.setdefault(key, []).append(event)
            for (caller_id, request_id), incoming in accepted.items():
                existing = self._db.execute(
                    "SELECT command_id,detail_json FROM command_events "
                    "WHERE caller_id=? AND request_id=? AND event_type='ACCEPTED' "
                    "ORDER BY receive_seq",
                    (caller_id, request_id),
                ).fetchall()
                if existing:
                    command_ids = [str(item["command_id"]) for item in incoming]
                    stored_ids = [str(row[0]) for row in existing]
                    incoming_hash = json.loads(str(incoming[0]["detail_json"])).get(
                        "request_fingerprint"
                    )
                    stored_hashes = {
                        json.loads(str(row[1] or "{}")).get("request_fingerprint")
                        for row in existing
                    }
                    if stored_ids != command_ids or stored_hashes != {incoming_hash}:
                        raise RepositoryError("request_id already accepted with other content")
            self._db.executemany(
                statement,
                [tuple(event.get(column) for column in columns) for event in events],
            )
            newest = max((int(item.get("receive_seq") or 0) for item in events), default=0)
            if newest:
                # UPSERT：全新库的 write-behind 首写也必须创建 meta 行，
                # 否则重启后 latest_receive_seq() 恒为 0、受理序号归零重复。
                self._db.execute(
                    "INSERT INTO repository_meta(key,value) VALUES('receive_seq',?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                    (str(newest),),
                )

        self._transaction(write)

    def _current_state(self, command_id: str) -> CommandState:
        row = self._db.execute(
            "SELECT state FROM command_events WHERE command_id=? "
            "ORDER BY event_seq DESC LIMIT 1",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return CommandState(row[0])

    def get(self, command_id: str) -> CommandResult | None:
        with self._lock:
            if self._closed or self._unavailable:
                raise RepositoryError("command journal is unavailable")
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
            device_id=first[4],
            point_id=first[5],
            value=json.loads(first[6]),
            config_reference=first[7],
            expires_at=datetime.fromisoformat(expires) if expires else None,
        )
        command = AcceptedCommand(
            command_id,
            first[2],
            first[3],
            spec,
            datetime.fromisoformat(first[1]),
            first[8],
        )
        last = rows[-1]
        state = CommandState(last[9])
        return CommandResult(
            command,
            state,
            CommandOutcome(last[10]) if last[10] else None,
            last[11],
            last[12],
            datetime.fromisoformat(last[1]) if state in TERMINAL_STATES else None,
            datetime.fromisoformat(last[1]),
        )

    def get_request(self, request_id: str, caller_id: str) -> tuple[CommandResult, ...]:
        """Return every accepted child in durable receive order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT command_id FROM command_events WHERE request_id=? "
                "AND caller_id=? AND event_type='ACCEPTED' ORDER BY receive_seq",
                (request_id, caller_id),
            ).fetchall()
        results = (self.get(command_id) for (command_id,) in rows)
        return tuple(result for result in results if result is not None)

    def recovery_decision(
        self, caller_id: str, request_id: str
    ) -> tuple[str, str, str, str, int, str] | None:
        with self._lock:
            if not self.operational:
                raise RepositoryError("command journal is unavailable")
            row = self._db.execute(
                "SELECT decision_id,device_id,revision,evidence_digest,"
                "fencing_epoch,token_id FROM recovery_decisions "
                "WHERE caller_id=? AND request_id=?",
                (caller_id, request_id),
            ).fetchone()
            return (
                (str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5]))
                if row is not None else None
            )

    def commit_recovery(
        self,
        *,
        caller_id: str,
        request_id: str,
        device_id: str,
        revision: str,
        evidence_digest: str,
        token_id: str,
        fencing_epoch: int,
        old_instance: str,
        new_instance: str,
    ) -> str:
        """Commit fenced recovery and an outbox event in one SQLite transaction."""
        def operation() -> str:
            previous = self._db.execute(
                "SELECT decision_id,device_id,revision,evidence_digest,"
                "fencing_epoch,token_id FROM recovery_decisions "
                "WHERE caller_id=? AND request_id=?",
                (caller_id, request_id),
            ).fetchone()
            if previous is not None:
                if tuple(previous[1:]) != (
                    device_id, revision, evidence_digest, fencing_epoch, token_id
                ):
                    raise FileExistsError("recovery request_id conflict")
                return str(previous[0])
            reused = self._db.execute(
                "SELECT 1 FROM recovery_decisions WHERE token_id=?", (token_id,)
            ).fetchone()
            if reused is not None:
                raise FileExistsError("recovery attestation already consumed")
            current = self._db.execute(
                "SELECT MAX(fencing_epoch) FROM recovery_decisions WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            if current is not None and fencing_epoch <= int(current):
                raise ValueError("recovery fencing epoch not increasing")
            self._allow_write_behind_flush()
            decision_id = str(uuid4())
            self._db.execute(
                "INSERT INTO recovery_decisions("
                "decision_id,caller_id,request_id,device_id,revision,"
                "evidence_digest,token_id,fencing_epoch,old_instance,new_instance,occurred_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, caller_id, request_id, device_id, revision,
                    evidence_digest, token_id, fencing_epoch, old_instance,
                    new_instance, utc_now().isoformat(),
                ),
            )
            return decision_id
        return self._transaction(operation)

    def pending_events(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            self._db.row_factory = sqlite3.Row
            try:
                rows = self._db.execute(
                    "SELECT * FROM command_events WHERE publish_state='PENDING' "
                    "AND event_type IN ('ACCEPTED','CONNECTING','EXECUTING','COMPLETED') "
                    "ORDER BY event_seq LIMIT ?",
                    (limit,),
                ).fetchall()
                events = [dict(row) for row in rows]
                remaining = max(0, limit - len(events))
                if remaining:
                    recovery = self._db.execute(
                        "SELECT decision_id AS event_id,device_id,caller_id,"
                        "request_id,revision,evidence_digest,token_id,"
                        "fencing_epoch,old_instance,new_instance,occurred_at,"
                        "publish_state,attempts FROM recovery_decisions "
                        "WHERE publish_state='PENDING' ORDER BY occurred_at LIMIT ?",
                        (remaining,),
                    ).fetchall()
                    for row in recovery:
                        event = dict(row)
                        event["event_type"] = "RECOVERY_UNBLOCKED"
                        event["command_id"] = event["decision_id"] if "decision_id" in event else event["event_id"]
                        events.append(event)
                return tuple(events)
            finally:
                self._db.row_factory = None

    def mark_published(self, event_id: str) -> None:
        def update() -> None:
            self._db.execute(
                "UPDATE command_events SET publish_state='PUBLISHED',"
                "published_at=? WHERE event_id=? AND publish_state='PENDING'",
                (utc_now().isoformat(), event_id),
            )
            self._db.execute(
                "UPDATE recovery_decisions SET publish_state='PUBLISHED',"
                "published_at=? WHERE decision_id=? AND publish_state='PENDING'",
                (utc_now().isoformat(), event_id),
            )

        self._transaction(update)

    def record_publish_failure(self, event_id: str) -> None:
        def update() -> None:
            self._db.execute(
                "UPDATE command_events SET attempts=attempts+1 WHERE event_id=?",
                (event_id,),
            )
            self._db.execute(
                "UPDATE recovery_decisions SET attempts=attempts+1 WHERE decision_id=?",
                (event_id,),
            )
        self._transaction(update)

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
                # T2 may have started physical I/O before its buffered
                # EXECUTING event reached SQLite. A missing T2 is not proof
                # that the original command was never sent.
                state = CommandState.UNKNOWN
                outcome = CommandOutcome.UNKNOWN
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
                """SELECT a.caller_id,a.request_id
                   FROM command_events AS a
                   WHERE a.event_type='ACCEPTED'
                   GROUP BY a.caller_id,a.request_id
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
            for caller_id, request_id in rows:
                command_rows = self._db.execute(
                    "SELECT command_id FROM command_events "
                    "WHERE request_id=? AND caller_id=? AND event_type='ACCEPTED'",
                    (request_id, caller_id),
                ).fetchall()
                for (command_id,) in command_rows:
                    self._db.execute(
                        "DELETE FROM command_events WHERE command_id=?",
                        (command_id,),
                    )
                    deleted += 1
            return deleted

        return self._transaction(purge)
