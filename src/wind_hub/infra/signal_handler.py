"""SIGHUP signal handler — sets an asyncio.Event, safe for signal context."""

from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


class ReloadSignalHandler:
    """Signal-safe SIGHUP handler.

    The OS signal handler (which runs in an interrupt-like context
    where you cannot await or allocate) does exactly one thing: calls
    ``loop.call_soon_threadsafe`` to set an ``asyncio.Event``.
    The actual reload work is driven by the event loop via ``await
    handler.wait()``.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def install(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Register the SIGHUP handler on the given (or running) event loop.

        On Windows, SIGHUP is not available and this method logs a
        warning — the handler can still be triggered manually via
        :meth:`trigger`.
        """
        if not hasattr(signal, "SIGHUP"):
            logger.warning("SIGHUP not available on this platform — " "reload via API/CLI only")
            return

        target_loop = loop or asyncio.get_running_loop()
        self._loop = target_loop

        def _on_signal() -> None:
            target_loop.call_soon_threadsafe(self._event.set)

        signal.signal(signal.SIGHUP, lambda signum, frame: _on_signal())
        logger.info("SIGHUP handler installed")

    def trigger(self) -> None:
        """Manually fire the reload event (for API/CLI triggers and tests)."""
        self._event.set()

    async def wait(self) -> None:
        """Block until a SIGHUP is received or :meth:`trigger` is called.

        After returning, the event is automatically cleared so the
        caller can loop on ``await handler.wait()``.
        """
        await self._event.wait()
        self._event.clear()
