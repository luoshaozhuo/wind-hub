"""CLI 最近一次诊断结果及原子 JSON 报告导出。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ResultStore:
    """会话内单一结果，避免隐藏状态和持久化协议。"""

    def __init__(self) -> None:
        self._last: dict[str, Any] | None = None

    @property
    def last(self) -> dict[str, Any] | None:
        return self._last

    def record(self, result: dict[str, Any]) -> None:
        self._last = result

    def show(self) -> dict[str, Any]:
        if self._last is None:
            return {
                "status": "UNKNOWN",
                "code": "NO_DIAGNOSIS_RESULT",
                "message": "No automatic diagnosis has run in this CLI session",
            }
        return self._last

    def export(self, path: str | Path) -> dict[str, Any]:
        if self._last is None:
            raise ValueError("no automatic diagnosis result to export")
        target = Path(path).expanduser().resolve()
        if target.suffix.lower() != ".json":
            raise ValueError("report filename must end with .json")
        if not target.parent.is_dir():
            raise ValueError(f"report directory does not exist: {target.parent}")
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "diagnosis": self._last,
        }
        payload = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=str,
        ).encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {
            "status": "PASS",
            "path": str(target),
            "bytes": len(payload),
            "diagnosis_status": self._last["status"],
        }
