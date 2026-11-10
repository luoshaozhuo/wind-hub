"""Crash-recoverable replacement of a group of configuration files.

The durable transaction marker is a write-ahead undo log.  Recovery always
rolls back an interrupted replacement before configuration is parsed.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4

from configuration.transaction import COMMIT_MARKER, COMMIT_STORE

_MARKER = COMMIT_MARKER
_STORE = COMMIT_STORE


@contextmanager
def _exclusive_config_lock(root: Path) -> Iterator[None]:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durable(path: Path, data: bytes) -> None:
    missing: list[Path] = []
    cursor = path.parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _sync_dir(directory.parent)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_dir(path.parent)


def _target(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not name
        or not relative.parts
        or relative.is_absolute()
        or relative.as_posix() != name
        or any(part in (".", "..") for part in relative.parts)
        or relative.parts[0].startswith(".")
    ):
        raise ValueError(f"invalid configuration relative path: {name!r}")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"configuration path escapes root: {name!r}")
    return target


def _recover_unlocked(root: Path) -> None:
    """Undo interrupted multi-file commit; safe to call repeatedly on startup."""
    root = root.resolve(strict=True)
    marker = root / _MARKER
    if not marker.exists():
        return
    record = json.loads(marker.read_text(encoding="utf-8"))
    identifier = record["transaction_id"]
    if not isinstance(identifier, str) or len(identifier) != 32:
        raise RuntimeError("invalid Commander configuration transaction identifier")
    folder = root / _STORE / identifier
    entries = record["files"]
    if not isinstance(entries, list):
        raise RuntimeError("invalid Commander transaction entries")
    # Preflight all backup material before modifying a single destination.
    restored: list[tuple[Path, bytes | None, int | None]] = []
    for entry in entries:
        name = entry["path"]
        target = _target(root, name)
        if entry["existed"]:
            backup = folder / "before" / name
            if not backup.is_file():
                raise RuntimeError(f"missing transaction rollback file: {name}")
            mode = entry.get("mode")
            if type(mode) is not int or not 0 <= mode <= 0o7777:
                raise RuntimeError(f"invalid rollback permissions: {name}")
            restored.append((target, backup.read_bytes(), mode))
        else:
            restored.append((target, None, None))
    for target, previous, mode in restored:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.rollback")
            _write_durable(temporary, previous)
            assert mode is not None
            temporary.chmod(mode)
            os.replace(temporary, target)
        _sync_dir(target.parent)
    marker.unlink()
    _sync_dir(root)
    shutil.rmtree(folder, ignore_errors=True)


def recover_config_commit(root: Path) -> None:
    """Recover under an exclusive lock shared with Core configuration readers."""
    root = root.resolve(strict=True)
    with _exclusive_config_lock(root):
        _recover_unlocked(root)


def commit_config_files(root: Path, changed: dict[str, bytes]) -> None:
    """Prevent cooperative readers from observing intermediate file replacements."""
    root = root.resolve(strict=True)
    with _exclusive_config_lock(root):
        _commit_unlocked(root, changed)


def _commit_unlocked(root: Path, changed: dict[str, bytes]) -> None:
    """Commit with an fsynced undo log; a crash before marker removal rolls back."""
    root = root.resolve(strict=True)
    _recover_unlocked(root)
    if not changed:
        return
    identifier = uuid4().hex
    folder = root / _STORE / identifier
    folder.mkdir(parents=True, exist_ok=False)
    _sync_dir(folder.parent)
    _sync_dir(root)
    marker = root / _MARKER
    entries: list[dict[str, str | bool | int | None]] = []
    armed = False
    try:
        for name, content in sorted(changed.items()):
            destination = _target(root, name)
            if not destination.parent.is_dir():
                destination.parent.mkdir(parents=True)
                _sync_dir(destination.parent)
            existed = destination.exists()
            mode = stat.S_IMODE(destination.stat().st_mode) if existed else None
            if existed:
                _write_durable(folder / "before" / name, destination.read_bytes())
            after = folder / "after" / name
            _write_durable(after, content)
            if mode is not None:
                after.chmod(mode)
            entries.append({"path": name, "existed": existed, "mode": mode})
        _sync_dir(folder)
        record = json.dumps(
            {"transaction_id": identifier, "files": entries},
            sort_keys=True,
        ).encode("utf-8")
        temp_marker = root / f".{_MARKER}.{identifier}.tmp"
        _write_durable(temp_marker, record)
        os.replace(temp_marker, marker)
        _sync_dir(root)
        armed = True
        for entry in entries:
            name = str(entry["path"])
            destination = _target(root, name)
            os.replace(folder / "after" / name, destination)
            _sync_dir(destination.parent)
        marker.unlink()
        _sync_dir(root)
        armed = False
    except BaseException:
        if armed:
            _recover_unlocked(root)
        raise
    finally:
        if not armed:
            shutil.rmtree(folder, ignore_errors=True)
