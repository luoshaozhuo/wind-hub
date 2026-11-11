"""Exclusive ownership of the shared Sink Service configuration Writer."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from sink_service.domain.config import ConfigurationError


class WriterLease:
    """Hold a process-level advisory lock throughout Writer lifetime."""

    def __init__(self, config_root: Path) -> None:
        self._root = config_root
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            raise ConfigurationError("Writer lease already acquired")
        self._root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._root / ".writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise ConfigurationError("another configuration Writer is active") from exc
        self._descriptor = fd

    def release(self) -> None:
        fd = self._descriptor
        self._descriptor = None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def __enter__(self) -> WriterLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
