"""Best-effort NATS Core broadcast of committed Configuration revisions."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading

import nats

_LOG = logging.getLogger(__name__)


class RevisionBroadcaster:
    """Decouple durable SQLite commits from non-durable change notifications."""

    def __init__(
        self,
        servers: tuple[str, ...],
        *,
        subject: str = "wh.config.changed",
        connect_timeout_ms: int = 2000,
        queue_capacity: int = 256,
    ) -> None:
        if not servers or subject != "wh.config.changed":
            raise ValueError("invalid NATS revision broadcast configuration")
        if connect_timeout_ms < 1 or queue_capacity < 1:
            raise ValueError("NATS broadcast limits must be positive")
        self._servers = servers
        self._subject = subject
        self._timeout = connect_timeout_ms / 1000
        # nats-py flush() is typed with an integer-second deadline; ceil so the
        # flush window is never shorter than the configured millisecond budget.
        self._flush_timeout = max(1, (connect_timeout_ms + 999) // 1000)
        self._queue: queue.Queue[int] = queue.Queue(maxsize=queue_capacity)
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="configuration-revision-broadcast", daemon=True
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("revision broadcaster already started")
        self._started = True
        self._thread.start()

    def enqueue(self, revision_id: int) -> None:
        """Never raise into a committed publication transaction."""
        if revision_id < 1:
            raise ValueError("revision_id must be positive")
        if not self._started or self._closing.is_set():
            _LOG.warning("NATS revision notice ignored during shutdown: %s", revision_id)
            return
        try:
            self._queue.put_nowait(revision_id)
        except queue.Full:
            _LOG.error("NATS revision notice dropped (queue full): revision=%s", revision_id)

    async def _publish_once(self, revision_id: int) -> None:
        client = await nats.connect(
            servers=list(self._servers),
            connect_timeout=self._timeout,
            max_reconnect_attempts=0,
        )
        try:
            payload = json.dumps({"revision_id": revision_id}, separators=(",", ":")).encode(
                "utf-8"
            )
            await client.publish(self._subject, payload)
            await client.flush(timeout=self._flush_timeout)
        finally:
            await client.close()

    def _run(self) -> None:
        while not self._closing.is_set():
            try:
                revision_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                asyncio.run(self._publish_once(revision_id))
            except Exception:
                _LOG.exception(
                    "NATS revision broadcast failed after SQLite commit: revision=%s",
                    revision_id,
                )
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 5.0) -> None:
        """Stop boundedly even when the pending notice queue is completely full."""
        if not self._started:
            return
        self._closing.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            _LOG.warning("revision broadcaster did not stop within shutdown deadline")
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            dropped += 1
        if dropped:
            _LOG.warning(
                "discarded %s best-effort NATS notices on shutdown; "
                "subscribers must reconcile their revision",
                dropped,
            )
