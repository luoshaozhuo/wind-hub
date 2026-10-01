"""IEC104 slave proxy TCP server.

Owns the listening socket and a set of live per-connection
:class:`IEC104SlaveSession` tasks.  :meth:`start` / :meth:`stop` are
idempotent; every connection shares the single :class:`DataSnapshot` via the
handlers, so one dispatch master's interrogation sees all collected values.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from wind_hub.adapter.inbound.iec104_slave.handlers import IEC104SlaveHandlers
from wind_hub.adapter.inbound.iec104_slave.session import IEC104SlaveSession
from wind_hub.domain.port.outbound import HealthStatus

logger = logging.getLogger(__name__)


class IEC104SlaveServer:
    """An IEC104 slave (server) listening for dispatch-master connections."""

    def __init__(
        self,
        host: str,
        port: int,
        handlers: IEC104SlaveHandlers,
        common_address: int,
    ) -> None:
        self._host = host
        self._port = port
        self._handlers = handlers
        self._common_address = common_address

        self._server: asyncio.Server | None = None
        self._sessions: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening (idempotent — a running server is a no-op)."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._on_client_connected,
            host=self._host,
            port=self._port,
        )
        logger.info(
            "IEC104 slave proxy listening on %s:%d (common_address=%d)",
            self._host,
            self._port,
            self._common_address,
        )

    async def stop(self) -> None:
        """Stop listening and cancel every live session (idempotent)."""
        if self._server is None:
            return
        self._server.close()
        for task in list(self._sessions):
            task.cancel()
        if self._sessions:
            await asyncio.gather(*list(self._sessions), return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None
        logger.info("IEC104 slave proxy stopped")

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    @property
    def session_count(self) -> int:
        """Number of currently live slave sessions."""
        return len(self._sessions)

    @property
    def port(self) -> int:
        """The bound listen port (resolves ``port=0`` to the actual port)."""
        if self._server is not None:
            sock = self._server.sockets[0]
            return int(sock.getsockname()[1])
        return self._port

    def health(self) -> HealthStatus:
        """Report proxy health — healthy while listening."""
        if self._server is None:
            return HealthStatus(healthy=False, message="not started")
        return HealthStatus(healthy=True, message=f"{self.session_count} session(s)")

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------

    async def _on_client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._track_session(task)

        session = IEC104SlaveSession(reader, writer, self._handlers, self._common_address)
        try:
            await session.run()
        except Exception:
            logger.warning("IEC104 slave session terminated with error", exc_info=True)
        finally:
            if task is not None:
                self._untrack_session(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _track_session(self, task: asyncio.Task[None]) -> None:
        self._sessions.add(task)

    def _untrack_session(self, task: asyncio.Task[None]) -> None:
        self._sessions.discard(task)
