"""Sink Service v1: isolated realtime or reliable process entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort
from sink_service.application.service_v1 import serve_control_plane
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot

logger = logging.getLogger(__name__)


class _PendingDataPlane:
    """Fail closed until a real Delivery adapter is wired in stage two/three."""

    def __init__(self, delivery_id: str) -> None:
        self._delivery_id = delivery_id

    async def start(self) -> None:
        raise ConfigurationError(
            f"Delivery {self._delivery_id}: data-plane factory is not implemented"
        )

    async def stop(self) -> None:
        return


def _stage_one_factory(
    definition: DeliveryDefinition, snapshot: ValidatedSnapshot
) -> DeliveryPort:
    """Stage-one control plane MUST NOT claim successful sample delivery."""
    return _PendingDataPlane(definition.delivery_id)


async def run_sink_service(config_root: Path, service_file: str) -> None:
    """Run one process; role and Writer ownership come exclusively from Service YAML."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            logger.warning("Signal handlers are unavailable on this platform")
    await serve_control_plane(config_root, service_file, _stage_one_factory, stop)


def main() -> None:
    parser = argparse.ArgumentParser(prog="wind-hub-sink-service")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--service-file", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_sink_service(args.config_root, args.service_file))


if __name__ == "__main__":
    main()
