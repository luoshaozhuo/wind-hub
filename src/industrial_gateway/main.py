"""Industrial Gateway v1 service process entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from core.configuration.local_selection import select_local_service
from core.infrastructure.configuration_cache import stage_selected_bundle
from core.infrastructure.configuration_repository import DeploymentObserver, SelectedBundle
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.adapters.inbound.grpc.data_server import (
    create_gateway_grpc_server,
)
from industrial_gateway.infrastructure.config.resolution import load_gateway
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots

logger = logging.getLogger(__name__)


async def run(config_path: Path, *, project_root: Path) -> None:
    """Run v1 listeners and gRPC service until a supported termination signal."""
    config = load_gateway(config_path, project_root=project_root)
    runtime = assemble_gateway(config)
    snapshots = ConfigSnapshots(runtime, config_path, project_root)
    observer: DeploymentObserver | None = None
    background: asyncio.Task[None] | None = None
    observer_stop = asyncio.Event()
    repository_endpoint = os.environ.get("WIND_HUB_CONFIGURATION_ENDPOINT", "")
    if repository_endpoint:
        selection = select_local_service(config_path)
        observer = DeploymentObserver(
            deployment_id=selection.deployment_id,
            service_id=selection.service_id,
            endpoint=repository_endpoint,
            token_env="WIND_HUB_CONFIGURATION_TOKEN",
            nats_servers=list(
                filter(None, os.environ.get("WIND_HUB_CONFIGURATION_NATS_SERVERS", "").split(","))
            ),
        )
        snapshots.observer = observer
        selected_observer = observer
        cache_root = Path(
            os.environ.get(
                "WIND_HUB_CONFIGURATION_CACHE",
                str(project_root / "state" / "gateway-configuration-cache"),
            )
        )

        async def apply_selected(bundle: SelectedBundle) -> bool:
            cached = await asyncio.to_thread(
                stage_selected_bundle,
                bundle,
                cache_root,
                selected_observer.deployment_id,
                selected_observer.service_id,
            )
            selected = await asyncio.to_thread(
                load_gateway, cached, project_root=cached.parents[2]
            )
            # Serialize Repository-selected Apply with local snapshot RPC Apply.
            # Never expose a new active snapshot before runtime convergence.
            async with snapshots.lock:
                # Static process/listener/security fields remain bound until restart.
                if selected.service != runtime.config.service:
                    return False
                results, restart = await runtime.reconfigure(selected)
                complete = not restart and all(item.status != "FAILED" for item in results)
                if complete:
                    snapshots.runtime_content = await asyncio.to_thread(cached.read_bytes)
                    snapshots.runtime_revision = selected.revision
                return complete

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
        if observer is not None:
            background = asyncio.create_task(observer.run(observer_stop, apply_selected))
        logger.info("Gateway v1 started: %d configured servers", len(runtime.instances))
        await shutdown.wait()
    finally:
        observer_stop.set()
        if background is not None:
            await background
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
