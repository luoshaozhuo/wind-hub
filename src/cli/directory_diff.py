"""Bounded and read-only comparison of two configuration directory trees."""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any

MAX_TEXT_BYTES = 1024 * 1024
MAX_DIFF_LINES = 10000
MAX_FILES = 10000
_SENSITIVE = re.compile(
    r"(?i)(password|passwd|token|secret|private[_-]?key|credential|api[_-]?key)"
)


def _inventory(root: Path, max_text_bytes: int) -> dict[str, tuple[int, str, bytes | None]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"not a regular configuration directory: {root}")
    items: dict[str, tuple[int, str, bytes | None]] = {}
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {file}")
        if not file.is_file():
            continue
        if len(items) >= MAX_FILES:
            raise ValueError(f"directory exceeds {MAX_FILES} files")
        before = file.stat()
        size = before.st_size
        digest = hashlib.sha256()
        text: bytes | None = b"" if size <= max_text_bytes else None
        with file.open("rb") as source:
            while chunk := source.read(65536):
                digest.update(chunk)
                if text is not None:
                    text += chunk
        after = file.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise RuntimeError(f"configuration changed during scan: {file}")
        items[file.relative_to(root).as_posix()] = (size, digest.hexdigest(), text)
    observed = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if observed != items.keys():
        raise RuntimeError(f"configuration directory changed during scan: {root}")
    return items


def _redact(lines: list[str], filename: str) -> list[str]:
    if filename.lower().endswith((".key", ".pem", ".p12", ".pfx")):
        return ["[SENSITIVE FILE REDACTED]"]
    result: list[str] = []
    for line in lines:
        key = line.partition(":")[0].partition("=")[0]
        if _SENSITIVE.search(key) or "-----BEGIN PRIVATE KEY-----" in line:
            result.append(key + ": [REDACTED]")
        else:
            result.append(line)
    return result


def compare_directories(
    left: Path,
    right: Path,
    *,
    max_file_bytes: int = MAX_TEXT_BYTES,
    max_output_lines: int = MAX_DIFF_LINES,
) -> dict[str, Any]:
    """Compare full relative file trees without loading service configuration."""
    if not 1 <= max_file_bytes <= MAX_TEXT_BYTES:
        raise ValueError("max_file_bytes must be in 1..1048576")
    if not 1 <= max_output_lines <= MAX_DIFF_LINES:
        raise ValueError("max_output_lines must be in 1..10000")
    try:
        a = _inventory(left, max_file_bytes)
        b = _inventory(right, max_file_bytes)
    except (OSError, RuntimeError) as exc:
        state = (
            "CHANGED_DURING_SCAN"
            if isinstance(exc, RuntimeError) and "changed during scan" in str(exc)
            else "INCOMPLETE"
        )
        return {
            "left": str(left),
            "right": str(right),
            "status": state,
            "complete": False,
            "error": str(exc),
            "added": [],
            "removed": [],
            "modified": [],
            "unchanged": None,
            "text_diff": {},
            "text_diff_truncated": False,
        }
    added = sorted(b.keys() - a.keys())
    removed = sorted(a.keys() - b.keys())
    modified = sorted(key for key in a.keys() & b.keys() if a[key][1] != b[key][1])
    unchanged = len(a.keys() & b.keys()) - len(modified)
    diffs: dict[str, str] = {}
    lines_remaining = max_output_lines
    truncated = False
    for key in modified:
        x, y = a[key][2], b[key][2]
        if x is None or y is None:
            continue
        if key.lower().endswith((".key", ".pem", ".p12", ".pfx")):
            # 两侧均存在且内容不同的敏感文件：保留修改可见性但不输出任何原文。
            diffs[key] = "[SENSITIVE FILE REDACTED]"
            continue
        try:
            old, new = x.decode("utf-8").splitlines(), y.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        lines = list(
            difflib.unified_diff(
                _redact(old, key),
                _redact(new, key),
                fromfile=f"a/{key}",
                tofile=f"b/{key}",
                lineterm="",
            )
        )
        take = min(lines_remaining, len(lines))
        if take:
            diffs[key] = chr(10).join(lines[:take])
            lines_remaining -= take
        truncated = truncated or take < len(lines)
    return {
        "left": str(left),
        "right": str(right),
        "status": "COMPLETE" if not truncated else "INCOMPLETE",
        "complete": not truncated,
        "left_files": len(a),
        "right_files": len(b),
        "left_bytes": sum(item[0] for item in a.values()),
        "right_bytes": sum(item[0] for item in b.values()),
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": unchanged,
        "text_diff": diffs,
        "text_diff_truncated": truncated,
    }
