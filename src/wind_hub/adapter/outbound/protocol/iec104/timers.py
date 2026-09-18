"""IEC 60870-5-104 timers.

Manages the three IEC104 timers (t1, t2, t3) using background
asyncio tasks.  Each timer has an associated async callback that
fires when the timer expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Sentinel to stop timer tasks cleanly.
_STOP = object()


class IEC104Timers:
    """IEC 60870-5-104 timer manager.

    Timers t1, t2, t3 are each backed by an ``asyncio.Task`` that
    sleeps for the configured duration and then invokes a callback.

    - **t1**: started when we send an I-frame; if it expires the
      peer has not acknowledged within the timeout → connection failure.
    - **t2**: started when we receive an I-frame; if it expires we send
      an S-frame acknowledgement (unless we already sent one).
    - **t3**: keep-alive; started when the connection enters STARTED.
      On expiry we send TESTFR act.  If no reply within t1 the
      connection is considered dead.
    """

    def __init__(
        self,
        t1: float = 15.0,
        t2: float = 10.0,
        t3: float = 20.0,
    ) -> None:
        self._t1 = t1
        self._t2 = t2
        self._t3 = t3

        # Callbacks — set after construction by the session.
        self.on_t1_timeout: Callable[[], Awaitable[None]] | None = None
        self.on_t2_timeout: Callable[[], Awaitable[None]] | None = None
        self.on_t3_timeout: Callable[[], Awaitable[None]] | None = None

        # Background tasks.
        self._t1_task: asyncio.Task[object] | None = None
        self._t2_task: asyncio.Task[object] | None = None
        self._t3_task: asyncio.Task[object] | None = None

        # Guards — only one timer-runner per timer at a time.
        self._t1_active = False
        self._t2_active = False
        self._t3_active = False

    # ==================================================================
    # public API
    # ==================================================================

    def start_t1(self) -> None:
        """(Re)start the t1 send-confirm timer."""
        self._start_timer(
            name="t1",
            delay=self._t1,
            task_attr="_t1_task",
            active_attr="_t1_active",
            callback_attr="on_t1_timeout",
        )

    def cancel_t1(self) -> None:
        """Cancel t1 — called when an ack arrives."""
        self._cancel_timer("t1", "_t1_task")

    def start_t2(self) -> None:
        """(Re)start the t2 ack-delay timer."""
        self._start_timer(
            name="t2",
            delay=self._t2,
            task_attr="_t2_task",
            active_attr="_t2_active",
            callback_attr="on_t2_timeout",
        )

    def cancel_t2(self) -> None:
        """Cancel t2 — called when an S-frame is sent."""
        self._cancel_timer("t2", "_t2_task")

    def start_t3(self) -> None:
        """(Re)start the t3 keep-alive timer."""
        self._start_timer(
            name="t3",
            delay=self._t3,
            task_attr="_t3_task",
            active_attr="_t3_active",
            callback_attr="on_t3_timeout",
        )

    def cancel_t3(self) -> None:
        """Cancel t3."""
        self._cancel_timer("t3", "_t3_task")

    async def stop_all(self) -> None:
        """Cancel all running timers.  Idempotent."""
        for attr in ("_t1_task", "_t2_task", "_t3_task"):
            task: asyncio.Task[object] | None = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def reset_all(self) -> None:
        """Cancel all timers without waiting (synchronous)."""
        self._cancel_timer("t1", "_t1_task")
        self._cancel_timer("t2", "_t2_task")
        self._cancel_timer("t3", "_t3_task")

    # ==================================================================
    # internals
    # ==================================================================

    def _start_timer(
        self,
        name: str,
        delay: float,
        task_attr: str,
        active_attr: str,
        callback_attr: str,
    ) -> None:
        # Cancel existing task first.
        self._cancel_timer(name, task_attr)

        task = asyncio.ensure_future(self._timer_runner(name, delay, active_attr, callback_attr))
        setattr(self, task_attr, task)

    def _cancel_timer(self, name: str, task_attr: str) -> None:
        task: asyncio.Task[object] | None = getattr(self, task_attr, None)
        if task is not None and not task.done():
            task.cancel()

    async def _timer_runner(
        self,
        name: str,
        delay: float,
        active_attr: str,
        callback_attr: str,
    ) -> None:
        try:
            setattr(self, active_attr, True)
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            setattr(self, active_attr, False)

        # Timer fired — invoke the callback.
        cb: Callable[[], Awaitable[None]] | None = getattr(self, callback_attr, None)
        if cb is not None:
            try:
                await cb()
            except Exception:
                logger.exception("IEC104 timer %s callback raised", name)
