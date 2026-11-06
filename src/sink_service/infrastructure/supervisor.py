"""Main-owned subprocess supervisor and bounded Unix-domain control sockets."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import socket
import struct
import tempfile
from multiprocessing.process import BaseProcess
from pathlib import Path

from sink_service.infrastructure.ipc import (
    ControlOperation,
    ControlReply,
    WorkerControlClient,
    read_frame,
)
from sink_service.infrastructure.worker import worker_main

logger = logging.getLogger(__name__)
ROLES = ("realtime", "reliable")


class WorkerSupervisor:
    """Own worker PIDs, IPC connections and configuration generation."""

    def __init__(self, root: Path, service_file: str) -> None:
        self._root = root
        self._service_file = service_file
        self._folder: tempfile.TemporaryDirectory[str] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._processes: dict[str, BaseProcess] = {}
        self._clients: dict[str, WorkerControlClient] = {}
        self._ready: dict[str, asyncio.Event] = {
            role: asyncio.Event() for role in ROLES
        }
        self._stopping = False
        self._socket_path = ""
        self.apply_timeout_seconds = 10.0

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            hello = ControlReply.from_wire(await asyncio.wait_for(read_frame(reader), 5))
            role = hello.evidence.get("role", "")
            if hello.request_id != "BOOT" or role not in ROLES:
                raise ValueError("invalid worker handshake")
            transport = writer.get_extra_info("socket")
            if transport is None:
                raise ValueError("missing Unix peer credentials")
            pid, uid, _gid = struct.unpack(
                "3i", transport.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            process = self._processes.get(role)
            if process is None or process.pid != pid or uid != os.getuid():
                raise ValueError("worker IPC peer PID/UID mismatch")
            if role in self._clients:
                raise ValueError("duplicate worker connection")
            self._clients[role] = WorkerControlClient(reader, writer)
            self._ready[role].set()
        except Exception:
            writer.close()
            await writer.wait_closed()
            logger.exception("rejected worker IPC handshake")

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("supervisor already started")
        self._folder = tempfile.TemporaryDirectory(prefix="windhub-sink-")
        self._socket_path = str(Path(self._folder.name) / "control.sock")
        self._server = await asyncio.start_unix_server(
            self._accept, path=self._socket_path
        )
        os.chmod(self._socket_path, 0o600)
        for role in ROLES:
            await self._spawn(role)

    async def _spawn(self, role: str) -> None:
        self._ready[role].clear()
        old_client = self._clients.pop(role, None)
        if old_client is not None:
            try:
                await asyncio.wait_for(old_client.close(), timeout=1)
            except (TimeoutError, OSError):
                logger.warning("old worker IPC failed to close cleanly: %s", role)
        process: BaseProcess = multiprocessing.get_context("spawn").Process(
            target=worker_main,
            args=(role, str(self._root), self._service_file, self._socket_path),
            daemon=False,
            name=f"windhub-sink-{role}",
        )
        process.start()
        self._processes[role] = process
        try:
            await asyncio.wait_for(self._ready[role].wait(), timeout=10)
        except TimeoutError:
            logger.error("worker %s failed to become ready", role)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 3)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 3)

    async def request(
        self, role: str, operation: ControlOperation, generation: str, *,
        delivery_id: str = "", timeout_seconds: float = 5.0,
    ) -> ControlReply:
        client = self._clients.get(role)
        process = self._processes.get(role)
        if client is None or process is None or not process.is_alive():
            return ControlReply(
                request_id="UNAVAILABLE", generation=generation, status="UNKNOWN",
                evidence={"error": f"{role} worker unavailable"},
            )
        return await client.request(
            operation, generation, delivery_id=delivery_id,
            timeout_seconds=timeout_seconds,
        )

    async def apply(self, generation: str) -> tuple[str, ...]:
        """Return converged delivery IDs only when both workers confirm."""
        replies = await asyncio.gather(
            *(
                self.request(
                    role, "APPLY", generation,
                    timeout_seconds=self.apply_timeout_seconds,
                )
                for role in ROLES
            ),
            return_exceptions=True,
        )
        applied: list[str] = []
        for reply in replies:
            if isinstance(reply, BaseException):
                logger.warning("worker apply IPC failed: %r", reply)
                continue
            if reply.status != "APPLIED":
                continue
            applied.extend(json.loads(reply.evidence.get("applied_ids", "[]")))
        return tuple(applied)

    async def supervise(self, stop: asyncio.Event) -> None:
        """Restart only a terminated worker, never duplicate a live role."""
        while not stop.is_set():
            for role, process in tuple(self._processes.items()):
                client = self._clients.get(role)
                if process.is_alive() and client is not None and client.broken:
                    logger.error("worker %s IPC broken; isolating its old PID", role)
                    process.terminate()
                    await asyncio.to_thread(process.join, 3)
                    if process.is_alive():
                        process.kill()
                        await asyncio.to_thread(process.join, 3)
                if process.is_alive():
                    continue
                process.join(timeout=0)
                if self._stopping:
                    continue
                logger.error("worker %s exited with code %s", role, process.exitcode)
                await self._spawn(role)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def close(self, generation: str) -> None:
        self._stopping = True
        await asyncio.gather(
            *(self.request(role, "SHUTDOWN", generation) for role in ROLES),
            return_exceptions=True,
        )
        for role, process in self._processes.items():
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                logger.error(
                    "UNSETTLED_WORKER_SHUTDOWN role=%s pid=%s; "
                    "native I/O completion is not confirmed", role, process.pid,
                )
                process.terminate()
                await asyncio.to_thread(process.join, 3)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 3)
        await asyncio.gather(
            *(client.close() for client in self._clients.values()),
            return_exceptions=True,
        )
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._folder is not None:
            self._folder.cleanup()
