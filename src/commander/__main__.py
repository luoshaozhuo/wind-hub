"""Commander gRPC service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc

from commander.assembly import CommanderSettingsSource, build_commander
from commander.infrastructure.adapters.inbound.grpc_transport import run_grpc
from core.application.protocol_registry import ProtocolRegistry
from core.configuration.local_selection import select_local_service
from core.infrastructure.configuration_cache import (
    load_active_bundle,
    mark_active_bundle,
    stage_selected_bundle,
)
from core.infrastructure.configuration_repository import DeploymentObserver, SelectedBundle
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
    observer: DeploymentObserver | None = None
    apply_selected: Callable[[SelectedBundle], Awaitable[bool]] | None = None
    selected_config = args.config
    cache_root: Path | None = None
    endpoint = os.environ.get("WIND_HUB_CONFIGURATION_ENDPOINT", "")
    if endpoint:
        # Bootstrap identity comes from the fixed local selector. The actual
        # Service content and its static transport must come from the
        # Repository-selected immutable version BEFORE process assembly.
        selection = select_local_service(args.config)
        bootstrap_settings = CommanderSettingsSource(args.config)()
        observer = DeploymentObserver(
            deployment_id=selection.deployment_id,
            service_id=selection.service_id,
            endpoint=endpoint,
            token_env="WIND_HUB_CONFIGURATION_TOKEN",
            nats_servers=list(bootstrap_settings.nats_servers),
        )
        cache_root = Path(
            os.environ.get(
                "WIND_HUB_CONFIGURATION_CACHE",
                str(bootstrap_settings.sqlite_path.parent / "configuration-cache"),
            )
        )
        try:
            bundle = asyncio.run(observer.fetch())
        except grpc.aio.AioRpcError as exc:
            if exc.code() not in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            ):
                raise
            # Only connectivity outages permit verified local recovery.
            selected_config, bundle = load_active_bundle(
                cache_root, observer.deployment_id, observer.service_id
            )
        except (OSError, TimeoutError):
            selected_config, bundle = load_active_bundle(
                cache_root, observer.deployment_id, observer.service_id
            )
        else:
            selected_config = stage_selected_bundle(
                bundle, cache_root, observer.deployment_id, observer.service_id
            )
        observer.target_revision = bundle.target_revision
        observer.repository_revision = bundle.repository_revision

    source = CommanderSettingsSource(selected_config)
    service = build_commander(
        service_config=selected_config,
        protocol_registry=registry,
        settings_source=source,
    )
    if observer is not None:
        assert cache_root is not None
        # No network listener is exposed before build and transport succeed.
        service.target_revision = observer.target_revision
        service.active_revision = observer.target_revision
        observer.active_revision = observer.target_revision
        selected_observer = observer

        async def apply_candidate(bundle: SelectedBundle) -> bool:
            assert cache_root is not None
            # The verified Repository target must be visible even if local
            # cache staging or validation fails before reload.
            service.target_revision = bundle.target_revision
            cached = await asyncio.to_thread(
                stage_selected_bundle,
                bundle,
                cache_root,
                selected_observer.deployment_id,
                selected_observer.service_id,
            )
            previous = source.path
            source.path = cached
            try:
                report = await service.reload()
            except BaseException:
                source.path = previous
                raise
            if report.blocked or report.restart_required:
                # A partially applied configuration cannot become the future
                # authoritative settings source of this still-running process.
                source.path = previous
                return False
            # Persist the verified selection only after the complete reload.
            mark_active_bundle(
                cache_root,
                selected_observer.deployment_id,
                selected_observer.service_id,
                bundle,
            )
            service.active_revision = bundle.target_revision
            return True

        apply_selected = apply_candidate

    def persist_started_generation() -> None:
        if observer is not None and cache_root is not None:
            mark_active_bundle(
                cache_root, observer.deployment_id, observer.service_id, bundle
            )

    asyncio.run(
        run_grpc(
            service,
            selected_config,
            observer=observer,
            apply_selected=apply_selected,
            on_started=persist_started_generation if observer is not None else None,
        )
    )


if __name__ == "__main__":
    main()
