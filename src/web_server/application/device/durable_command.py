"""跨重启的 Web 控制命令防重记录；保守的 at-most-once 语义。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

_ReserveResult = tuple[Literal["new", "pending", "completed"], str | None]


class DurableCommandRegistry:
    """先持久保留客户端 ID，再发送一次 RPC；未知结果不重发。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _connection(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS commands ("
            "command_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT)"
        )
        return connection

    def _reserve(self, command_id: str, fingerprint: str) -> _ReserveResult:
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, result_json FROM commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ValueError("command_id has already been used for different content")
                return ("completed", row[1]) if row[1] is not None else ("pending", None)
            connection.execute(
                "INSERT INTO commands(command_id, fingerprint) VALUES (?, ?)",
                (command_id, fingerprint),
            )
            return "new", None

    def _finish(self, command_id: str, fingerprint: str, result: str) -> None:
        with closing(self._connection()) as connection, connection:
            cursor = connection.execute(
                "UPDATE commands SET result_json=? "
                "WHERE command_id=? AND fingerprint=? AND result_json IS NULL",
                (result, command_id, fingerprint),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("durable command record changed before completion")

    async def reserve(self, command_id: str, fingerprint: str) -> _ReserveResult:
        async with self._lock:
            return await asyncio.to_thread(self._reserve, command_id, fingerprint)

    async def finish(self, command_id: str, fingerprint: str, result: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._finish, command_id, fingerprint, result)
