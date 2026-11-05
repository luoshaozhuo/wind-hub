"""Sink Service Main: one gRPC server and two separate worker processes."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import UTC, datetime
from pathlib import Path

from sink_service.application.config import read_snapshot
from sink_service.application.config_manager import ConfigManager
from sink_service.application.observability import Observability
from sink_service.infrastructure.grpc_credentials import server_credentials
from sink_service.infrastructure.config_repository import GenerationStore
from sink_service.infrastructure.grpc_v1 import build_v1_server
from sink_service.infrastructure.supervisor import WorkerSupervisor
from sink_service.infrastructure.worker_registry import WorkerRegistryView
from sink_service.infrastructure.writer_lease import WriterLease

logger = logging.getLogger(__name__)
SERVICE_FILE = "configs/services/sink_service.yaml"


async def run_sink_service(config_root: Path, service_file: str = SERVICE_FILE) -> None:
    if service_file != SERVICE_FILE:
        raise ValueError("canonical Service file required")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            logger.warning("signal handlers unavailable")
    store = GenerationStore(config_root)
    lease = WriterLease(config_root)
    lease.acquire()
    supervisor = WorkerSupervisor(config_root, service_file)
    server = None
    background: list[asyncio.Task[None]] = []
    try:
        if (config_root / "CURRENT").exists():
            snapshot = store.load(service_file)
        else:
            snapshot = read_snapshot(config_root, config_root / service_file)
            store.commit(snapshot, "", old=None)
        if Path(snapshot.service["config_root"]).resolve() != config_root.resolve():
            raise ValueError("configured config_root differs from runtime")
        registry = WorkerRegistryView(supervisor, snapshot)
        manager = ConfigManager(
            store, snapshot, writer=True, reconcile=registry.apply_configuration
        )
        service = snapshot.service
        telemetry = Observability(
            service["runtime"]["max_event_records"],
            window_ms=service["metrics"]["metrics_window_ms"],
        )
        grpc_config = service["grpc"]
        server = build_v1_server(
            config=manager, registry=registry,
            service_id=service["service_id"], role="main",
            credentials=server_credentials(service["security"]),
            address=f"{grpc_config['listen_host']}:{grpc_config['listen_port']}",
            max_active_rpcs=grpc_config["max_active_rpcs"],
            max_request_bytes=grpc_config["max_request_bytes"],
            callers=service["security"]["callers"],
            observability=telemetry,
        )
        await server.start()
        await supervisor.start()
        await registry.refresh()
        background.append(asyncio.create_task(supervisor.supervise(stop)))
        background.append(asyncio.create_task(
            _poll_workers(registry, telemetry, stop, service["metrics"]["metrics_compute_interval_ms"])
        ))
        await stop.wait()
    finally:
        stop.set()
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if server is not None:
            await server.stop(grace=3)
        try:
            await supervisor.close(
                manager.disk.revision if "manager" in locals() else ""
            )
        finally:
            lease.release()


async def _poll_workers(
    registry: WorkerRegistryView,
    telemetry: Observability,
    stop: asyncio.Event,
    interval_ms: int,
) -> None:
    """Bounded periodic metrics cycle; gRPC only reads precomputed snapshots."""
    loop = asyncio.get_running_loop()
    period = interval_ms / 1000
    next_tick = loop.time() + period
    previous_health: dict[str, str] = {}
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=max(0.0, next_tick - loop.time())
            )
        except TimeoutError:
            pass
        if stop.is_set():
            return
        now = loop.time()
        next_tick += max(1, int((now - next_tick) / period) + 1) * period
        try:
            await registry.refresh()
            for role, state in registry.worker_health.items():
                if previous_health.get(role) != state:
                    telemetry.event(
                        "ERROR" if state != "RUNNING" else "INFO",
                        role, "WORKER_STATE", state,
                    )
            previous_health = dict(registry.worker_health)
            metrics = await registry.collect_metrics()
            if metrics is None:
                telemetry.mark_unavailable()
                continue
            totals, deliveries, latencies, incomplete = metrics
            telemetry.sample(
                totals,
                latency_observations=latencies,
                latency_overflow_at=datetime.now(UTC) if incomplete else None,
                delivery_counters=deliveries,
            )
        except Exception:
            logger.exception("worker metrics cycle failed")
            telemetry.mark_unavailable()


def main() -> None:
    parser = argparse.ArgumentParser(prog="wind-hub-sink-service")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--service-file", default=SERVICE_FILE)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_sink_service(args.config_root, args.service_file))


if __name__ == "__main__":
    main()
