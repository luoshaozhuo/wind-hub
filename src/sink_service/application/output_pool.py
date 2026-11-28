"""Application ownership of shared Sink output resources and unsettled I/O."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from sink_service.application.config import template_document as _template
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot


class _PhysicalCapacity:
    """Single-loop dynamic cap: use the minimum among all bound Sink owners."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.inflight = 0
        self._changed = asyncio.Event()

    async def acquire(self) -> bool:
        while True:
            if self.inflight < self.limit:
                self.inflight += 1
                return True
            # No await between checking the count and clearing the signal.
            self._changed.clear()
            await self._changed.wait()

    def release(self) -> None:
        if self.inflight <= 0:
            raise RuntimeError("physical I/O permit released twice")
        self.inflight -= 1
        self._changed.set()

    def resize(self, limit: int) -> None:
        self.limit = limit
        self._changed.set()


class _PhysicalQuota:
    """Physical destination with a shared cap, owners and cancellation quarantine."""

    def __init__(self, limit: int) -> None:
        self.quota = _PhysicalCapacity(limit)
        self.limit = limit
        self.limits: dict[str, int] = {}
        self.references = 0
        self.unsafe = False
        self.tasks: set[asyncio.Task[BatchOutcome]] = set()

    def register(self, sink_id: str, limit: int) -> None:
        self.limits[sink_id] = limit
        self.limit = min(self.limits.values())
        self.quota.resize(self.limit)

    def unregister(self, sink_id: str) -> None:
        self.limits.pop(sink_id)
        if self.limits:
            self.limit = min(self.limits.values())
            self.quota.resize(self.limit)


class _SinkOutput:
    """Shared adapter, ownership count, and a physical Sink concurrency cap."""

    def __init__(
        self, adapter: Any, signature: str, physical_key: str, physical: _PhysicalQuota
    ) -> None:
        self.adapter = adapter
        self.signature = signature
        self.physical_key = physical_key
        self.physical = physical
        self.references = 0
        # 本 Sink 自有的在途原生写。release 的结算检查只看自己的写；
        # 共享同一物理目标的兄弟 Delivery 在途写不得触发错误栅栏。
        self.tasks: set[asyncio.Task[BatchOutcome]] = set()

    @property
    def quota(self) -> _PhysicalCapacity:
        return self.physical.quota

    @property
    def unsafe(self) -> bool:
        return self.physical.unsafe

    @unsafe.setter
    def unsafe(self, value: bool) -> None:
        self.physical.unsafe = value


class _QuotaWriter:
    """Keep the global/Sink quota while native I/O is not settled."""

    def __init__(
        self,
        adapter: Any,
        global_quota: asyncio.Semaphore,
        sink_quota: _PhysicalCapacity,
        timeout_ms: int,
        output: _SinkOutput,
    ) -> None:
        self._adapter = adapter
        self._global_quota = global_quota
        self._sink_quota = sink_quota
        self._timeout = timeout_ms / 1000
        self._output = output
        self._pending: asyncio.Task[BatchOutcome] | None = None
        self._pending_timings: tuple[float, float, float] | None = None

    def _finish(self, task: asyncio.Task[BatchOutcome]) -> None:
        if task.cancelled():
            # Cancellation is not proof that a native write has stopped.
            # Quarantine the physical output and retain both permits until
            # an external fence or process termination proves safety.
            self._output.unsafe = True
            return
        self._output.tasks.discard(task)
        self._output.physical.tasks.discard(task)
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
        if self._output.unsafe:
            return BatchOutcome((), (), "physical Sink output quarantined after cancellation")
        pending = self._pending
        if pending is not None and not pending.done():
            return BatchOutcome((), (), "previous Sink I/O remains unsettled")
        global_started = time.monotonic()
        try:
            await asyncio.wait_for(self._global_quota.acquire(), timeout=self._timeout)
        except TimeoutError:
            return BatchOutcome((), (), "global I/O quota exhausted")
        global_seconds = time.monotonic() - global_started
        physical_started = time.monotonic()
        try:
            await asyncio.wait_for(self._sink_quota.acquire(), timeout=self._timeout)
        except BaseException:
            self._global_quota.release()
            raise

        physical_seconds = time.monotonic() - physical_started
        # A competing operation can quarantine this physical target while we
        # wait for the semaphore. Recheck *after* ownership is acquired.
        if self._output.unsafe:
            self._sink_quota.release()
            self._global_quota.release()
            return BatchOutcome((), (), "physical Sink output quarantined after cancellation")

        async def run_operation() -> BatchOutcome:
            return await operation()

        native_started = time.monotonic()
        try:
            task = asyncio.create_task(run_operation())
        except BaseException:
            self._sink_quota.release()
            self._global_quota.release()
            raise
        self._pending = task
        self._pending_timings = (global_seconds, physical_seconds, native_started)
        self._output.tasks.add(task)
        self._output.physical.tasks.add(task)
        task.add_done_callback(self._finish)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
            return replace(
                result,
                quota_stage_seconds=(
                    global_seconds,
                    physical_seconds,
                    time.monotonic() - native_started,
                ),
            )
        except TimeoutError:
            return BatchOutcome((), (), "Sink I/O result uncertain; waiting for settlement")

    async def settle(self) -> BatchOutcome | None:
        """Wait for native settlement even after the caller's observation timeout."""
        task = self._pending
        if task is None:
            return None
        try:
            result = await asyncio.shield(task)
        except Exception as exc:
            result = BatchOutcome((), (), f"underlying I/O failed: {exc}")
        timings = self._pending_timings
        if timings is None:
            return result
        global_seconds, physical_seconds, native_started = timings
        return replace(
            result,
            quota_stage_seconds=(
                global_seconds,
                physical_seconds,
                time.monotonic() - native_started,
            ),
        )


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
        self._physical_quotas: dict[str, _PhysicalQuota] = {}
        self._lock = asyncio.Lock()
        self._quota: asyncio.Semaphore | None = None

    @staticmethod
    def _physical_key(sink_id: str, snapshot: ValidatedSnapshot) -> str:
        """Identify configured physical destinations independently of Sink IDs."""
        sink = snapshot.sinks[sink_id]
        template = _template(snapshot, "sinks", sink["template_id"], sink["template_version"])
        kind = template["kind"]
        connection = sink["connection"]
        identity: tuple[Any, ...]
        if kind == "redis":
            identity = (kind, connection["host"], connection["port"])
        elif kind == "kafka":
            identity = (kind, sorted(connection["bootstrap_servers"]))
        elif kind == "gateway":
            identity = (kind, connection["endpoint"])
        elif kind == "modbus_tcp":
            identity = (kind, connection["listen_host"], connection["port"])
        else:
            raise ConfigurationError(f"unsupported Sink type: {kind}")
        return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))

    async def acquire(self, sink_id: str, snapshot: ValidatedSnapshot) -> Any:
        async with self._lock:
            adapter, signature = self._build(sink_id, snapshot)
            physical_key = self._physical_key(sink_id, snapshot)
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
                shared = self._physical_quotas.get(physical_key)
                if shared is not None and shared.unsafe:
                    await adapter.close()
                    raise ConfigurationError("physical Sink target is quarantined")
                if shared is None:
                    shared = _PhysicalQuota(limit)
                    self._physical_quotas[physical_key] = shared
                shared.register(sink_id, limit)
                shared.references += 1
                current = _SinkOutput(adapter, signature, physical_key, shared)
                self._entries[sink_id] = current
            elif current.signature != signature:
                raise ConfigurationError(f"Sink {sink_id} still in use with a different binding")
            if current.unsafe:
                raise ConfigurationError("physical Sink output is quarantined")
            current.references += 1
            if self._quota is None:
                self._quota = asyncio.Semaphore(snapshot.service["runtime"]["max_inflight_global"])
            return _QuotaWriter(
                current.adapter,
                self._quota,
                current.quota,
                snapshot.service["runtime"]["io_wait_timeout_ms"],
                current,
            )

    async def release(self, sink_id: str) -> None:
        async with self._lock:
            current = self._entries[sink_id]
            if current.unsafe:
                raise ConfigurationError("cancelled native Sink I/O requires external fencing")
            if current.references > 1:
                current.references -= 1
                return
            await current.adapter.close()
            # Adapter.close may yield while native I/O finishes or is
            # cancelled. Only this Sink's own native writes can be unsettled
            # here; sibling Deliveries sharing the physical target keep their
            # own in-flight tasks and must not be fenced by this release.
            if any(not task.done() or task.cancelled() for task in current.tasks):
                current.unsafe = True
            if current.unsafe:
                raise ConfigurationError("cancelled native Sink I/O requires external fencing")
            del self._entries[sink_id]
            physical = current.physical
            physical.references -= 1
            physical.unregister(sink_id)
            if physical.references == 0:
                del self._physical_quotas[current.physical_key]
