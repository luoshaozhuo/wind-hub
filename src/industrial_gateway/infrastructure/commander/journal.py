"""保守型控制日志：写入前持久登记，确认终局后清除。

日志中残留的命令意味着可能已经执行，重启后保持闭锁；
只允许停机并经现场核对后人工清理日志。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class CommandJournal:
    """单事件循环中同步提交短小文件；确保控制 RPC 前完成 fsync。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pending: dict[str, str] = {}
        self._failed = False
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in data.items()
            ):
                raise ValueError("invalid command journal")
            self._pending = data

    def blocked(self, key: str) -> bool:
        return self._failed or key in self._pending

    def begin(self, key: str, command_id: str) -> None:
        if self.blocked(key):
            raise RuntimeError(f"control remains locked: {key}")
        self._pending[key] = command_id
        try:
            self._save()
        except BaseException:
            self._failed = True
            raise

    def confirmed(self, key: str) -> None:
        if self._failed:
            raise RuntimeError("command journal persistence failed; control locked")
        if key in self._pending:
            del self._pending[key]
            try:
                self._save()
            except BaseException:
                self._failed = True
                raise

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps(self._pending, sort_keys=True).encode("utf-8")
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
