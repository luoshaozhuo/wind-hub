"""Best-effort Configuration revision hints over NATS Core.

Publishing is asynchronous after the SQLite transaction. A NATS outage cannot
rollback or invalidate a successfully published configuration revision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import Sequence

import nats

_LOG = logging.getLogger(__name__)
_SUBJECT = "wh.config.changed"


class NatsRevisionPublisher:
    """Bounded fire-and-forget hint producer; consumers reconcile by revision."""

    def __init__(self, *, servers: Sequence[str], capacity: int = 128) -> None:
        if not servers or capacity < 1:
            raise ValueError("Configuration NATS endpoints and queue required")
        self._servers = list(servers)
        self._queue: queue.Queue[int] = queue.Queue(maxsize=capacity)
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is not None or self._closed.is_set():
            raise RuntimeError("NATS publisher already running or closed")
        self._worker = threading.Thread(
            target=self._run, name="configuration-revision-publisher", daemon=True
        )
        self._worker.start()

    def enqueue(self, revision_id: int) -> None:
        """Never block a successful Configuration publish on its notification."""
        if revision_id <= 0:
            raise ValueError("revision_id must be positive")
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(revision_id)
        except queue.Full:
            _LOG.warning("Configuration NATS notification backlog full; periodic polling required")

    def _run(self) -> None:
        async def publish_all() -> None:
            while not self._closed.is_set():
                try:
                    revision = await asyncio.to_thread(self._queue.get, True, 1.0)
                except queue.Empty:
                    continue
                connection = None
                try:
                    connection = await nats.connect(
                        servers=self._servers,
                        connect_timeout=2,
                        max_reconnect_attempts=0,
                    )
                    payload = json.dumps(
                        {"revision_id": revision},
                        sort_keys=True, separators=(",", ":"),
                    ).encode()
                    await connection.publish(_SUBJECT, payload)
                    await connection.flush(timeout=3)
                except Exception:
                    _LOG.warning(
                        "Configuration revision %d hint not delivered; "
                        "downstream periodic reconciliation remains authoritative",
                        revision,
                    )
                finally:
                    if connection is not None:
                        await connection.close()

        asyncio.run(publish_all())

    def close(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self._closed.set()
        worker.join(timeout=10)
        if worker.is_alive():
            _LOG.warning("NATS notification worker did not exit before shutdown deadline")
        self._worker = None
