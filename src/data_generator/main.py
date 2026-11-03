"""Data Generator service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from data_generator.application.config import load_settings
from data_generator.application.runtime import Runtime
from data_generator.application.snapshots import recover_transaction
from data_generator.infrastructure.server import make_server

logger = logging.getLogger(__name__)


async def run(config_path: Path) -> None:
    recover_transaction(config_path.absolute().parent.parent)
    settings = load_settings(config_path)
    runtime = Runtime(settings)
    bind = settings.service.get("service", {}).get("grpc_bind", "127.0.0.1:50061")
    server = make_server(runtime, bind)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handler unavailable: %s", signum)
    try:
        await server.start()
        await shutdown.wait()
    finally:
        await server.stop(grace=3.0)
        await runtime.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(prog="wind-hub-data-generator")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        recover_transaction(args.config.absolute().parent.parent)
        load_settings(args.config)
        return 0
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
