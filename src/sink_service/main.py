"""Independent Sink Service process, without Collector imports."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from hashlib import sha256
from pathlib import Path

from contracts.sample_message import DeliveryMode
from sink_service.assembly import assemble_sinks
from sink_service.application.runtime import SinkRuntime, SinkState
from sink_service.application.management import SinkManager
from sink_service.infrastructure.grpc_management import build_management_server
from sink_service.infrastructure.intent import SinkIntentStore

logger = logging.getLogger(__name__)


async def run_sink_service(
    config_dir: Path,
    *,
    servers: tuple[str, ...],
    delivery: DeliveryMode | str,
    realtime_sinks: frozenset[str] = frozenset(),
    reliable_sinks: frozenset[str] = frozenset(),
    wind_farm_id: str,
    shutdown_timeout: float = 30.0,
    grpc_listen: str = '127.0.0.1:50074',
    state_dir: Path = Path('/var/tmp/wind-hub/sink-state'),
) -> None:
    """Start each Sink independently and shut down on a process signal."""
    state_name = sha256(f"{wind_farm_id}:{delivery}".encode()).hexdigest()[:32]
    store = SinkIntentStore(state_dir / f"{state_name}.json")
    if delivery == "both":
        if not realtime_sinks or not reliable_sinks:
            raise ValueError("both mode requires explicit realtime and reliable sink lists")
        if realtime_sinks & reliable_sinks:
            raise ValueError("one Sink cannot consume both delivery channels")
        sinks = {
            **assemble_sinks(
                config_dir, servers=servers, delivery="realtime",
                wind_farm_id=wind_farm_id, intent_store=store,
                sink_ids=realtime_sinks,
            ),
            **assemble_sinks(
                config_dir, servers=servers, delivery="reliable",
                wind_farm_id=wind_farm_id, intent_store=store,
                sink_ids=reliable_sinks,
            ),
        }
    else:
        sinks = assemble_sinks(
            config_dir, servers=servers, delivery=delivery,
            wind_farm_id=wind_farm_id, intent_store=store,
            sink_ids=realtime_sinks or reliable_sinks or None,
        )
    manager = SinkManager(sinks)
    grpc_server = build_management_server(manager, grpc_listen)
    await grpc_server.start()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            logger.warning("Event loop does not support signal handlers")
    for sink_id, sink in sinks.items():
        if not store.should_start(sink_id):
            continue
        try:
            await sink.start()
        except Exception:
            logger.exception("Sink %s failed to start", sink_id)
    async def restore_failed() -> None:
        while not stopping.is_set():
            try:
                await asyncio.wait_for(stopping.wait(), timeout=5.0)
                return
            except TimeoutError:
                pass
            for sink_id, sink in sinks.items():
                if sink.desired_running and sink.state is SinkState.FAILED:
                    try:
                        await sink.start()
                    except Exception:
                        logger.exception("Sink %s recovery failed", sink_id)

    supervisor = asyncio.create_task(restore_failed())
    await stopping.wait()
    await grpc_server.stop(grace=5.0)
    supervisor.cancel()
    try:
        await supervisor
    except asyncio.CancelledError:
        pass

    async def close(sink_id: str, sink: SinkRuntime) -> None:
        try:
            await asyncio.wait_for(sink.stop(explicit=False), timeout=shutdown_timeout)
        except Exception:
            logger.exception("Sink %s shutdown failed", sink_id)
    await asyncio.gather(*(close(name, sink) for name, sink in sinks.items()))


def main() -> None:
    """Run the Sink Service from a standalone command."""
    parser = argparse.ArgumentParser(prog="wind-hub-sink-service")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--nats", action="append", required=True)
    parser.add_argument("--delivery", choices=("realtime", "reliable", "both"), required=True)
    parser.add_argument("--realtime-sink", action="append", default=[])
    parser.add_argument("--reliable-sink", action="append", default=[])
    parser.add_argument("--wind-farm-id", required=True)
    parser.add_argument("--grpc-listen", default="127.0.0.1:50074")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/tmp/wind-hub/sink-state"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_sink_service(
            args.config,
            servers=tuple(args.nats),
            delivery=args.delivery,
            realtime_sinks=frozenset(args.realtime_sink),
            reliable_sinks=frozenset(args.reliable_sink),
            wind_farm_id=args.wind_farm_id,
            state_dir=args.state_dir,
            grpc_listen=args.grpc_listen,
        )
    )


if __name__ == "__main__":
    main()
