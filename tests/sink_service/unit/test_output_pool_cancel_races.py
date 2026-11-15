"""Deterministic regression evidence for physical-target cancellation races."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from sink_service.application.output_pool import (
    SharedSinkOutputPool,
    _PhysicalQuota,
    _QuotaWriter,
    _SinkOutput,
)
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.safety]


class _GatedAdapter:
    def __init__(self) -> None:
        self.started = 0
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release_writes = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def write_batch(self, samples: tuple[Any, ...]) -> BatchOutcome:
        self.started += 1
        self.first_started.set()
        if self.started >= 2:
            self.second_started.set()
        await self.release_writes.wait()
        return BatchOutcome(tuple(range(len(samples))), tuple(range(len(samples))))

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()


def _writer(
    adapter: _GatedAdapter,
    output: _SinkOutput,
    global_quota: asyncio.Semaphore,
) -> _QuotaWriter:
    return _QuotaWriter(adapter, global_quota, output.quota, 1000, output)


async def test_quarantine_rechecks_waiters_before_starting_native_io() -> None:
    """A waiter must not enter the native adapter after another write is cancelled."""
    adapter = _GatedAdapter()
    physical = _PhysicalQuota(2)
    output = _SinkOutput(adapter, "signature", "redis://target", physical)
    global_quota = asyncio.Semaphore(3)
    writers = [_writer(adapter, output, global_quota) for _ in range(3)]

    first = asyncio.create_task(writers[0].write_batch((object(),)))
    second = asyncio.create_task(writers[1].write_batch((object(),)))
    await asyncio.wait_for(adapter.second_started.wait(), timeout=1)
    waiting = asyncio.create_task(writers[2].write_batch((object(),)))
    await asyncio.sleep(0)

    pending = writers[0]._pending
    assert pending is not None
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    await asyncio.sleep(0)
    assert output.unsafe

    adapter.release_writes.set()
    await asyncio.wait_for(second, timeout=1)
    await asyncio.wait_for(waiting, timeout=1)

    assert adapter.started == 2, "quarantined target submitted a queued native write"


async def test_release_cannot_recycle_quarantine_during_close() -> None:
    """Cancellation during adapter.close must preserve the physical quarantine."""
    adapter = _GatedAdapter()
    physical = _PhysicalQuota(1)
    physical.references = 1
    output = _SinkOutput(adapter, "signature", "redis://target", physical)
    output.references = 1
    pool = SharedSinkOutputPool(lambda _sink_id, _snapshot: (adapter, "signature"))
    pool._entries["sink"] = output
    pool._physical_quotas[output.physical_key] = physical
    writer = _writer(adapter, output, asyncio.Semaphore(1))

    writing = asyncio.create_task(writer.write_batch((object(),)))
    await asyncio.wait_for(adapter.first_started.wait(), timeout=1)
    releasing = asyncio.create_task(pool.release("sink"))
    await asyncio.wait_for(adapter.close_started.wait(), timeout=1)

    pending = writer._pending
    assert pending is not None
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await writing
    await asyncio.sleep(0)
    assert output.unsafe

    adapter.release_close.set()
    with contextlib.suppress(ConfigurationError):
        await asyncio.wait_for(releasing, timeout=1)

    assert pool._entries.get("sink") is output
    assert pool._physical_quotas.get(output.physical_key) is physical
    assert pool._physical_quotas[output.physical_key].unsafe


@pytest.mark.parametrize("cancel_native", [False, True])
async def test_release_observes_native_state_when_done_callback_runs_after_close(
    cancel_native: bool,
) -> None:
    """close must inspect native completion, not depend on callback scheduling."""
    adapter = _GatedAdapter()
    physical = _PhysicalQuota(1)
    physical.references = 1
    physical.register("sink", 1)
    output = _SinkOutput(adapter, "signature", "redis://target", physical)
    output.references = 1
    pool = SharedSinkOutputPool(lambda _sink_id, _snapshot: (adapter, "signature"))
    pool._entries["sink"] = output
    pool._physical_quotas[output.physical_key] = physical
    writer = _writer(adapter, output, asyncio.Semaphore(1))

    writing = asyncio.create_task(writer.write_batch((object(),)))
    await asyncio.wait_for(adapter.first_started.wait(), timeout=1)
    pending = writer._pending
    assert pending is not None
    # Deterministically hold the completion callback until after close returns.
    assert pending.remove_done_callback(writer._finish) == 1
    releasing = asyncio.create_task(pool.release("sink"))
    await asyncio.wait_for(adapter.close_started.wait(), timeout=1)

    if cancel_native:
        pending.cancel()
    else:
        adapter.release_writes.set()
    with contextlib.suppress(asyncio.CancelledError):
        await writing
    adapter.release_close.set()
    if cancel_native:
        with pytest.raises(ConfigurationError, match="external fencing"):
            await asyncio.wait_for(releasing, timeout=1)
        assert pool._entries.get("sink") is output
        assert pool._physical_quotas.get(output.physical_key) is physical
        assert output.unsafe
    else:
        await asyncio.wait_for(releasing, timeout=1)
        assert not output.unsafe
        assert "sink" not in pool._entries
        assert output.physical_key not in pool._physical_quotas

    # Deliver the held callback afterwards; it must not change the close verdict.
    pending.add_done_callback(writer._finish)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert output.unsafe is cancel_native
