"""Data Generator service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from data_generator.application.runtime import Runtime
from data_generator.application.config_policy import requires_service_restart
from core.configuration.local_selection import select_local_service
from core.infrastructure.configuration_repository import DeploymentObserver, SelectedBundle
from core.infrastructure.configuration_cache import stage_selected_bundle
from data_generator.infrastructure.adapters.inbound.server import make_server
from data_generator.infrastructure.adapters.outbound.csv_history import CsvHistoryReader
from data_generator.infrastructure.adapters.outbound.gateway import GatewayWriter
from data_generator.infrastructure.adapters.outbound.recording import JsonlRecordWriter
from data_generator.infrastructure.config.service_settings import load_settings
from data_generator.infrastructure.gateway_tls import gateway_credentials
from data_generator.infrastructure.logging import configure_logging
from data_generator.infrastructure.persistence.snapshots import recover_transaction

logger = logging.getLogger(__name__)


async def run(config_path: Path) -> None:
    recover_transaction(config_path.absolute().parent.parent)
    settings = load_settings(config_path)
    output = settings.service.get("output", {})
    buffer = settings.service.get("buffer", {})
    writer = GatewayWriter(
        timeout=float(output.get("grpc_timeout_seconds", 3.0)),
        max_points=int(buffer.get("max_points_per_batch", 1000)),
        max_inflight=int(output.get("max_inflight", 1)),
        gateway_tls=output.get("gateway_tls"),
    )
    runtime = Runtime(
        settings, writer, recorder=JsonlRecordWriter(), history_reader=CsvHistoryReader()
    )
    observer: DeploymentObserver | None = None
    background: asyncio.Task[None] | None = None
    observer_stop = asyncio.Event()
    endpoint = os.environ.get("WIND_HUB_CONFIGURATION_ENDPOINT", "")
    if endpoint:
        selection = select_local_service(config_path)
        observer = DeploymentObserver(
            deployment_id=selection.deployment_id,
            service_id=selection.service_id,
            endpoint=endpoint,
            token_env="WIND_HUB_CONFIGURATION_TOKEN",
            nats_servers=list(
                filter(None, os.environ.get("WIND_HUB_CONFIGURATION_NATS_SERVERS", "").split(","))
            ),
        )
        cache_root = Path(
            os.environ.get(
                "WIND_HUB_CONFIGURATION_CACHE",
                str(settings.root.parent / "state" / "data-generator-configuration-cache"),
            )
        )

        async def apply_selected(bundle: SelectedBundle) -> bool:
            cached = await asyncio.to_thread(
                stage_selected_bundle,
                bundle,
                cache_root,
                observer.deployment_id,
                observer.service_id,
            )
            candidate = await asyncio.to_thread(load_settings, cached)
            if requires_service_restart(runtime.settings.service, candidate.service):
                return False
            await runtime.commit_configuration(lambda: (candidate, False))
            # A running generation remains bound to its original Scenario.
            active_states = {"STARTING", "RUNNING", "STOPPING"}
            return all(
                state.actual_state not in active_states
                or state.scenario == candidate.scenarios.get(state.scenario.scenario_id)
                for state in runtime.generations.values()
            )

    bind = settings.service.get("service", {}).get("grpc_bind", "127.0.0.1:50061")
    server = make_server(runtime, bind, observer)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handler unavailable: %s", signum)
    try:
        await server.start()
        if observer is not None:
            background = asyncio.create_task(observer.run(observer_stop, apply_selected))
        await shutdown.wait()
    finally:
        observer_stop.set()
        if background is not None:
            await background
        await server.stop(grace=3.0)
        await runtime.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(prog="wind-hub-data-generator")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        recover_transaction(args.config.absolute().parent.parent)
        settings = load_settings(args.config)
        # --check must fail if the configured client certificate bundle is
        # unavailable, not merely if the YAML paths look syntactically valid.
        gateway_credentials(settings.service.get("output", {}).get("gateway_tls"))
        return 0
    configure_logging()
    asyncio.run(run(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
