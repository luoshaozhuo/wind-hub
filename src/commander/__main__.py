"""Commander gRPC service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from commander.assembly import CommanderSettingsSource, build_commander
from commander.infrastructure.adapters.inbound.grpc_transport import run_grpc
from core.application.protocol_registry import ProtocolRegistry
from core.configuration.local_selection import select_local_service
from core.infrastructure.configuration_repository import DeploymentObserver, SelectedBundle
from core.infrastructure.configuration_cache import stage_selected_bundle
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver


def main() -> None:
    parser = argparse.ArgumentParser(description="wind-hub Commander gRPC service")
    parser.add_argument(
        "--config", required=True, type=Path, help="Commander service YAML pathname"
    )
    args = parser.parse_args()
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)
    source = CommanderSettingsSource(args.config)
    service = build_commander(
        service_config=args.config,
        protocol_registry=registry,
        settings_source=source,
    )
    observer: DeploymentObserver | None = None
    apply_selected: Callable[[SelectedBundle], Awaitable[bool]] | None = None
    endpoint = os.environ.get("WIND_HUB_CONFIGURATION_ENDPOINT", "")
    if endpoint:
        selection = select_local_service(args.config)
        observer = DeploymentObserver(
            deployment_id=selection.deployment_id,
            service_id=selection.service_id,
            endpoint=endpoint,
            token_env="WIND_HUB_CONFIGURATION_TOKEN",
            nats_servers=list(source().nats_servers),
        )
        cache_root = Path(
            os.environ.get(
                "WIND_HUB_CONFIGURATION_CACHE",
                str(source().sqlite_path.parent / "configuration-cache"),
            )
        )

        async def apply_candidate(bundle: SelectedBundle) -> bool:
            cached = await asyncio.to_thread(
                stage_selected_bundle,
                bundle,
                cache_root,
                observer.deployment_id,
                observer.service_id,
            )
            previous = source.path
            source.path = cached
            try:
                report = await service.reload()
            except Exception:
                source.path = previous
                raise
            service.target_revision = bundle.target_revision
            if report.blocked or report.restart_required:
                return False
            service.active_revision = bundle.target_revision
            return True

        apply_selected = apply_candidate

    asyncio.run(
        run_grpc(service, args.config, observer=observer, apply_selected=apply_selected)
    )


if __name__ == "__main__":
    main()
