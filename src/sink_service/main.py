"""Independent Sink Service process, without Collector imports."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from contracts.sample_message import DeliveryMode
from sink_service.assembly import assemble_sinks
from sink_service.application.runtime import SinkRuntime

logger = logging.getLogger(__name__)


async def run_sink_service(
    config_dir: Path,
    *,
    servers: tuple[str, ...],
    delivery: DeliveryMode,
    wind_farm_id: str,
    shutdown_timeout: float = 30.0,
) -> None:
    """Start each Sink independently and shut down on a process signal."""
    sinks = assemble_sinks(
        config_dir, servers=servers, delivery=delivery, wind_farm_id=wind_farm_id,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            logger.warning("Event loop does not support signal handlers")
    for sink_id, sink in sinks.items():
        try:
            await sink.start()
        except Exception:
            logger.exception("Sink %s failed to start", sink_id)
    await stopping.wait()
    async def close(sink_id: str, sink: SinkRuntime) -> None:
        try:
            await asyncio.wait_for(sink.stop(), timeout=shutdown_timeout)
        except Exception:
            logger.exception("Sink %s shutdown failed", sink_id)
    await asyncio.gather(*(close(name, sink) for name, sink in sinks.items()))


def main() -> None:
    """Run the Sink Service from a standalone command."""
    parser = argparse.ArgumentParser(prog="wind-hub-sink-service")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--nats", action="append", required=True)
    parser.add_argument("--delivery", choices=("realtime", "reliable"), required=True)
    parser.add_argument("--wind-farm-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_sink_service(
            args.config,
            servers=tuple(args.nats),
            delivery=args.delivery,
            wind_farm_id=args.wind_farm_id,
        )
    )


if __name__ == "__main__":
    main()
