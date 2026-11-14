"""Industrial Gateway v1 service process entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.config.resolution import load_gateway
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots
from industrial_gateway.infrastructure.adapters.inbound.grpc.data_server import (
    create_gateway_grpc_server,
)

logger = logging.getLogger(__name__)


async def run(config_path: Path, *, project_root: Path) -> None:
    """Run v1 listeners and gRPC service until a supported termination signal."""
    config = load_gateway(config_path, project_root=project_root)
    runtime = assemble_gateway(config)
    snapshots = ConfigSnapshots(runtime, config_path, project_root)
    server = create_gateway_grpc_server(runtime, snapshots)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handler not supported: %s", signum)
    try:
        await server.start()
        await runtime.start()
        logger.info("Gateway v1 started: %d configured servers", len(runtime.instances))
        await shutdown.wait()
    finally:
        try:
            # Stop accepting new RPCs before releasing protocol resources.
            await server.stop(grace=0)
        finally:
            await runtime.stop()


def main() -> int:
    """Parse an explicit service file and deployment root (never derive cwd)."""
    parser = argparse.ArgumentParser(prog="wind-hub-gateway")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_gateway(args.config, project_root=args.project_root)
    if args.check:
        # Runtime construction validates adapter capabilities without binding.
        assemble_gateway(config)
        logger.info("Gateway v1 config valid: %d servers", len(config.servers))
        return 0
    asyncio.run(run(args.config, project_root=args.project_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
