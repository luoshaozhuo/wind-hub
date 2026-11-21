"""Version-bound Collector Task control persistence via SQLite."""

from __future__ import annotations
import sqlite3
from pathlib import Path

class SqliteTaskIntentStore:
    """Store user intentions without mutating published configuration."""

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Task intent database cannot be a symlink")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_intents "
                "(device_id TEXT NOT NULL, task_version TEXT NOT NULL, "
                "desired_state TEXT NOT NULL CHECK(desired_state IN ('START','STOP')), "
                "PRIMARY KEY (device_id, task_version))"
            )

    def read_intents(self, file_version: str) -> dict[str, str]:
        """Read only intents associated with the selected Task identity and version."""
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT device_id, desired_state FROM task_intents WHERE task_version=?",
                (file_version,),
            )
            return dict(rows.fetchall())

    def write_intent(self, device_id: str, file_version: str, state: str) -> None:
        """Persist a validated intent in one durable SQLite transaction."""
        if not device_id or not file_version or state not in {"START", "STOP"}:
            raise ValueError("invalid Collector Task intent")
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute(
                "INSERT INTO task_intents VALUES (?, ?, ?) "
                "ON CONFLICT(device_id, task_version) "
                "DO UPDATE SET desired_state=excluded.desired_state",
                (device_id, file_version, state),
            )
