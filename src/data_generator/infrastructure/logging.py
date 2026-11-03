"""Compact JSON process logging without sample values or secret fields."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Only allow predefined tracing fields in the public log envelope."""

    _fields = ("source_id", "batch_sequence", "device_id", "trace_id", "stage")

    def format(self, record: logging.LogRecord) -> str:
        content: dict[str, str | int] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in self._fields:
            value = getattr(record, name, None)
            if isinstance(value, str | int) and not isinstance(value, bool):
                content[name] = value
        if record.exc_info:
            content["exception"] = self.formatException(record.exc_info)
        return json.dumps(content, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
