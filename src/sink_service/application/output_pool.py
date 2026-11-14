"""Application ownership of shared Sink output resources and unsettled I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sink_service.application.config import template_document as _template
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot


class _SinkOutput:
    """Shared adapter, ownership count, and a physical Sink concurrency cap."""

    def __init__(self, adapter: Any, signature: str, capacity: int) -> None:
        self.adapter = adapter
        self.signature = signature
        self.references = 0
        self.quota = asyncio.Semaphore(capacity)


class _QuotaWriter:
    """Keep the global/Sink quota while native I/O is not settled."""

    def __init__(
        self,
        adapter: Any,
        global_quota: asyncio.Semaphore,
        sink_quota: asyncio.Semaphore,
        timeout_ms: int,
    ) -> None:
        self._adapter = adapter
        self._global_quota = global_quota
        self._sink_quota = sink_quota
        self._timeout = timeout_ms / 1000
        self._pending: asyncio.Task[BatchOutcome] | None = None

    def _finish(self, task: asyncio.Task[BatchOutcome]) -> None:
        if not task.cancelled():
            task.exception()
        self._sink_quota.release()
        self._global_quota.release()

    @property
    def io_inflight(self) -> bool:
        task = self._pending
        return task is not None and not task.done()

    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        return await self._start_write(lambda: self._adapter.write_batch(samples))

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        """Apply the same Sink/global quotas to pre-encoded Kafka records."""
        return await self._start_write(
            lambda: self._adapter.write_encoded_batch(payloads, key=key, stripe=stripe)
        )

    async def _start_write(self, operation: Callable[[], Awaitable[BatchOutcome]]) -> BatchOutcome:
        pending = self._pending
        if pending is not None and not pending.done():
            return BatchOutcome((), (), "previous Sink I/O remains unsettled")
        try:
            await asyncio.wait_for(self._global_quota.acquire(), timeout=self._timeout)
        except TimeoutError:
            return BatchOutcome((), (), "global I/O quota exhausted")
        try:
            await asyncio.wait_for(self._sink_quota.acquire(), timeout=self._timeout)
        except BaseException:
            self._global_quota.release()
            raise

        async def run_operation() -> BatchOutcome:
            return await operation()

        try:
            task = asyncio.create_task(run_operation())
        except BaseException:
            self._sink_quota.release()
            self._global_quota.release()
            raise
        self._pending = task
        task.add_done_callback(self._finish)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except TimeoutError:
            return BatchOutcome((), (), "Sink I/O result uncertain; waiting for settlement")

    async def settle(self) -> BatchOutcome | None:
        """Wait for native settlement even after the caller's observation timeout."""
        task = self._pending
        if task is None:
            return None
        try:
            return await asyncio.shield(task)
        except Exception as exc:
            return BatchOutcome((), (), f"underlying I/O failed: {exc}")


def template_capabilities(snapshot: ValidatedSnapshot, sink_id: str) -> dict[str, Any]:
    sink = snapshot.sinks[sink_id]
    template = _template(snapshot, "sinks", sink["template_id"], sink["template_version"])
    capabilities = template["capabilities"]
    if not isinstance(capabilities, dict):
        raise ConfigurationError("Sink capabilities must be a mapping")
    return capabilities


class SharedSinkOutputPool:
    """Share one physical Sink between multiple Delivery owners in one process."""

    def __init__(
        self,
        builder: Callable[[str, ValidatedSnapshot], tuple[Any, str]],
    ) -> None:
        self._build = builder
        self._entries: dict[str, _SinkOutput] = {}
        self._lock = asyncio.Lock()
        self._quota: asyncio.Semaphore | None = None

    async def acquire(self, sink_id: str, snapshot: ValidatedSnapshot) -> Any:
        async with self._lock:
            adapter, signature = self._build(sink_id, snapshot)
            current = self._entries.get(sink_id)
            if current is None:
                await adapter.open()
                capabilities = template_capabilities(snapshot, sink_id)
                limit = min(
                    snapshot.service["runtime"]["max_inflight_per_sink"],
                    capabilities["max_safe_inflight"],
                )
                if not capabilities["supports_concurrent_write"]:
                    limit = 1
                current = _SinkOutput(adapter, signature, limit)
                self._entries[sink_id] = current
            elif current.signature != signature:
                raise ConfigurationError(f"Sink {sink_id} still in use with a different binding")
            current.references += 1
            if self._quota is None:
                self._quota = asyncio.Semaphore(snapshot.service["runtime"]["max_inflight_global"])
            return _QuotaWriter(
                current.adapter,
                self._quota,
                current.quota,
                snapshot.service["runtime"]["io_wait_timeout_ms"],
            )

    async def release(self, sink_id: str) -> None:
        async with self._lock:
            current = self._entries[sink_id]
            if current.references > 1:
                current.references -= 1
                return
            await current.adapter.close()
            del self._entries[sink_id]


