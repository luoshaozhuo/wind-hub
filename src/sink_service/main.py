"""Sink Service v1: isolated realtime or reliable process entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort
from sink_service.domain.config import ValidatedSnapshot
from sink_service.infrastructure.bootstrap import serve_control_plane
from sink_service.infrastructure.reliable_runtime import ReliableFactory
from sink_service.infrastructure.realtime_runtime import RealtimeFactory

logger = logging.getLogger(__name__)


async def run_sink_service(
    config_root: Path, service_file: str, peer_service_file: str | None = None
) -> None:
    """Run one process; role and Writer ownership come exclusively from Service YAML."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            logger.warning("Signal handlers are unavailable on this platform")
    realtime_factory = RealtimeFactory()
    reliable_factory = ReliableFactory()

    def factory(definition: DeliveryDefinition, snapshot: ValidatedSnapshot) -> DeliveryPort:
        if definition.channel == "realtime":
            return realtime_factory(definition, snapshot)
        return reliable_factory(definition, snapshot)

    await serve_control_plane(
        config_root, service_file, factory, stop,
        peer_service_file=peer_service_file,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="wind-hub-sink-service")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--service-file", required=True)
    parser.add_argument(
        "--peer-service-file",
        help="Explicit second Service file, required only for initial generation",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_sink_service(args.config_root, args.service_file, args.peer_service_file)
    )


if __name__ == "__main__":
    main()
