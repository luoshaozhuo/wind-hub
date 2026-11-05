"""Sink data worker subprocess with a dedicated asyncio event loop."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from sink_service.application.delivery_registry import DeliveryRegistry
from sink_service.infrastructure.config_repository import GenerationStore
from sink_service.infrastructure.ipc import ControlReply, ControlRequest, read_frame, write_frame
from sink_service.infrastructure.realtime_runtime import RealtimeFactory
from sink_service.infrastructure.reliable_runtime import ReliableFactory

logger = logging.getLogger(__name__)


async def run_worker(role: str, root: Path, service_file: str, socket_path: str) -> None:
    store = GenerationStore(root)
    snapshot = store.load(service_file)
    registry = DeliveryRegistry(role, snapshot)
    factory = RealtimeFactory() if role == "realtime" else ReliableFactory()
    await registry.apply_configuration(snapshot, factory)
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await write_frame(writer, ControlReply(
        request_id="BOOT", generation=snapshot.revision,
        status="APPLIED", evidence={"role": role},
    ).to_wire())
    try:
        while True:
            request = ControlRequest.from_wire(await read_frame(reader))
            status = "APPLIED"
            evidence: dict[str, str] = {}
            try:
                if request.operation == "SHUTDOWN":
                    await registry.shutdown()
                elif request.operation in {"APPLY", "START", "STOP"}:
                    updated = store.load(service_file)
                    if updated.revision != request.generation:
                        raise ValueError("generation mismatch")
                    if updated.service != snapshot.service:
                        raise ValueError("RESTART_REQUIRED")
                    applied = await registry.apply_configuration(updated, factory)
                    snapshot = updated
                    evidence["applied_ids"] = json.dumps(applied)
                    expected = {item.delivery_id for item in registry.list()}
                    if not expected.issubset(set(applied)):
                        status = "PENDING"
                elif request.operation == "GET_STATUS":
                    evidence["deliveries"] = json.dumps(
                        [asdict(item) for item in registry.list()]
                    )
                elif request.operation == "GET_METRICS":
                    evidence["counters"] = json.dumps(registry.counters())
                elif request.operation == "DIAGNOSE":
                    evidence["details"] = json.dumps(
                        registry.inspect(request.delivery_id)
                        if request.delivery_id else {}
                    )
            except Exception as exc:
                logger.exception("worker command failed")
                status = "FAILED"
                evidence["error"] = str(exc)
            await write_frame(writer, ControlReply(
                request_id=request.request_id, generation=request.generation,
                status=status, evidence=evidence,
            ).to_wire())
            if request.operation == "SHUTDOWN":
                break
    finally:
        await registry.shutdown()
        writer.close()
        await writer.wait_closed()


def worker_main(role: str, root: str, service_file: str, socket_path: str) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(role, Path(root), service_file, socket_path))
