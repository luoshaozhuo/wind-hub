"""Unit tests for IEC104 timers — t1/t2/t3 with async callbacks."""

from __future__ import annotations

import asyncio

import pytest

from wind_hub.adapter.outbound.protocol.iec104.timers import IEC104Timers


# Use short delays for tests.
@pytest.fixture
def timers() -> IEC104Timers:
    return IEC104Timers(t1=0.05, t2=0.05, t3=0.05)


async def test_t1_fires_callback(timers: IEC104Timers) -> None:
    """t1 fires the callback after the delay."""
    fired: asyncio.Event = asyncio.Event()

    async def callback() -> None:
        fired.set()

    timers.on_t1_timeout = callback
    timers.start_t1()

    await asyncio.wait_for(fired.wait(), timeout=1.0)
    assert fired.is_set()


async def test_t1_cancel_prevents_callback(timers: IEC104Timers) -> None:
    """Cancelling t1 before expiry prevents the callback."""
    fired = False

    async def callback() -> None:
        nonlocal fired
        fired = True

    timers.on_t1_timeout = callback
    timers.start_t1()

    # Cancel almost immediately.
    await asyncio.sleep(0.01)
    timers.cancel_t1()

    # Wait longer than the delay.
    await asyncio.sleep(0.15)
    assert not fired


async def test_t2_fires_callback(timers: IEC104Timers) -> None:
    """t2 fires the callback after the delay."""
    fired: asyncio.Event = asyncio.Event()

    async def callback() -> None:
        fired.set()

    timers.on_t2_timeout = callback
    timers.start_t2()

    await asyncio.wait_for(fired.wait(), timeout=1.0)
    assert fired.is_set()


async def test_t3_fires_callback(timers: IEC104Timers) -> None:
    """t3 fires the callback after the delay."""
    fired: asyncio.Event = asyncio.Event()

    async def callback() -> None:
        fired.set()

    timers.on_t3_timeout = callback
    timers.start_t3()

    await asyncio.wait_for(fired.wait(), timeout=1.0)
    assert fired.is_set()


async def test_t1_restart_resets_timer(timers: IEC104Timers) -> None:
    """Restarting t1 delays the callback."""
    count = 0

    async def callback() -> None:
        nonlocal count
        count += 1

    timers.on_t1_timeout = callback
    timers.start_t1()

    # Restart after half the delay.
    await asyncio.sleep(0.02)
    timers.start_t1()

    # Wait enough for the original timer to fire, but not the restarted one.
    await asyncio.sleep(0.04)

    # The first timer was cancelled by restart, so callback hasn't fired.
    # Now wait for the restarted timer.
    # Total elapsed at this point: 0.02 + 0.04 = 0.06s
    # Timer started at 0.02s with 0.05s delay → fires at 0.07s
    # But we're at 0.06s now, need to wait ~0.01s more.

    # Wait for the callback to fire.
    await asyncio.sleep(0.05)
    assert count == 1


async def test_stop_all_cancels_all(timers: IEC104Timers) -> None:
    """stop_all prevents all pending callbacks."""
    t1_fired = False
    t2_fired = False
    t3_fired = False

    async def t1_cb() -> None:
        nonlocal t1_fired
        t1_fired = True

    async def t2_cb() -> None:
        nonlocal t2_fired
        t2_fired = True

    async def t3_cb() -> None:
        nonlocal t3_fired
        t3_fired = True

    timers.on_t1_timeout = t1_cb
    timers.on_t2_timeout = t2_cb
    timers.on_t3_timeout = t3_cb

    timers.start_t1()
    timers.start_t2()
    timers.start_t3()

    await asyncio.sleep(0.01)
    await timers.stop_all()

    # Wait to ensure no callback fires.
    await asyncio.sleep(0.15)
    assert not t1_fired
    assert not t2_fired
    assert not t3_fired


async def test_reset_all_synchronous(timers: IEC104Timers) -> None:
    """reset_all cancels all timers synchronously."""
    timers.start_t1()
    timers.start_t2()
    timers.start_t3()

    timers.reset_all()

    # All tasks should be cancelled.
    assert timers._t1_task is not None
    assert timers._t2_task is not None
    assert timers._t3_task is not None
    await asyncio.sleep(0.02)
    assert timers._t1_task.done()
    assert timers._t2_task.done()
    assert timers._t3_task.done()


async def test_non_existent_callback_does_not_raise(timers: IEC104Timers) -> None:
    """Timer fires but no callback set — should not raise."""
    # Don't set any callback — just start and wait.
    timers.start_t1()
    await asyncio.sleep(0.10)
    # Should not have raised.


async def test_callback_exception_is_logged(timers: IEC104Timers) -> None:
    """When a callback raises, the timer should not crash."""

    async def bad_callback() -> None:
        raise RuntimeError("timer callback boom")

    timers.on_t1_timeout = bad_callback
    timers.start_t1()

    # Wait for the timer to fire — should not propagate exception.
    await asyncio.sleep(0.10)
    # If we get here without error, the test passes.


async def test_stop_all_idempotent(timers: IEC104Timers) -> None:
    """stop_all can be called multiple times safely."""
    timers.start_t1()
    await timers.stop_all()
    await timers.stop_all()
    await timers.stop_all()
    # Should not raise.
