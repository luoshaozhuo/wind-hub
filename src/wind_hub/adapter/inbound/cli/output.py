"""CLI output formatting — JSON and simple human-readable tables.

Deliberately avoids ``rich`` so the CLI keeps a minimal dependency footprint;
tables use plain character alignment, which is sufficient for operator-facing
output and easy to consume from scripts.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _fmt(value: Any) -> str:
    """Render a scalar value for a table cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def print_json(obj: Any) -> None:
    """Print ``obj`` as indented JSON to stdout.

    ``default=str`` keeps the output valid for values pydantic's JSON mode
    would otherwise leave as Python objects (e.g. ``datetime``).
    """
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """Print ``rows`` as an aligned table with the given ``columns``.

    Column widths are derived from the header and cell contents; missing keys
    render as empty cells.
    """
    if not columns:
        return
    data = [[_fmt(row.get(col)) for col in columns] for row in rows]
    widths = [len(col) for col in columns]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True)).rstrip()

    print(_line(list(columns)))
    print("  ".join("-" * w for w in widths))
    for row in data:
        print(_line(row))


def print_kv(data: dict[str, Any]) -> None:
    """Print ``data`` as aligned ``key: value`` lines."""
    if not data:
        return
    width = max(len(str(key)) for key in data)
    for key, value in data.items():
        print(f"{str(key).ljust(width)}  {_fmt(value)}")


def print_error(message: str) -> None:
    """Print an error message to stderr, coloured red when stderr is a TTY."""
    if sys.stderr.isatty():
        print(f"\033[31m{message}\033[0m", file=sys.stderr)
    else:
        print(message, file=sys.stderr)


__all__ = ["print_json", "print_table", "print_kv", "print_error"]
