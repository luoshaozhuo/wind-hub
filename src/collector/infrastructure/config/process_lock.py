"""Single-process ownership of a Collector configuration state directory."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def collector_state_owner(root: Path) -> Iterator[None]:
    """Prevent concurrent Collector processes from mutating the same CURRENT."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Collector state directory is already in use") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
