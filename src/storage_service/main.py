"""Storage Service process lifecycle: Configuration-pinned reliable Kafka ingestion."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

import grpc

from storage_service.application.wide_ingestion import WideIngestionService
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.query import CursorCodec, HistoryQueryService
from storage_service.application.wide_query import WideCursorCodec, WideHistoryQueryService
from storage_service.application.tasks import AsyncStorageTasks
from storage_service.infrastructure.adapters.inbound.grpc_server import (
    build_storage_grpc_server,
)
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
    ImmutableConfigArchive,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
)
from storage_service.infrastructure.adapters.outbound.configuration_snapshot import (
    StorageBundleSnapshot,
)
from storage_service.infrastructure.adapters.outbound.configuration_watch import (
    StorageConfigurationWatcher,
)
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineBackend,
)
from storage_service.infrastructure.adapters.outbound.tdengine_backup import (
    NativeTDengineBackup,
)
from storage_service.settings import StorageBootstrap, load_bootstrap

_LOG = logging.getLogger(__name__)


async def run_service(bootstrap: StorageBootstrap) -> None:
    """Start only with a fully validated immutable configuration closure."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            _LOG.warning("signal handlers are unavailable in this environment")

    base = bootstrap.cache.directory
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive = ImmutableConfigArchive(base / "configuration-archive")
    with MetadataStore(base / "storage_metadata.sqlite3") as metadata:
        metadata.interrupt_unfinished()
        configuration = ConfigurationGrpcClient(
            endpoint=bootstrap.configuration.endpoint,
            token_env=bootstrap.configuration.token_env,
            archive=archive,
        )
        await configuration.open()
        try:
            snapshot = StorageBundleSnapshot(base / "configuration-snapshots")
            restored_from_snapshot = False
            restored_pending_target = False
            try:
                bundle = await configuration.fetch_service_bundle(
                    deployment_id=bootstrap.deployment_id, service_id=bootstrap.service_id
                )
            except (grpc.aio.AioRpcError, TimeoutError, ConnectionError) as exc:
                if isinstance(exc, grpc.aio.AioRpcError) and exc.code() not in (
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                ):
                    raise
                bundle = await asyncio.to_thread(snapshot.load)
                # A verified static target is a candidate for the next process
                # generation, not the currently running active generation.
                pending_path = base / "configuration-snapshots" / "target_bundle.json"
                if pending_path.exists() or pending_path.is_symlink():
                    pending = await asyncio.to_thread(snapshot.load_target)
                    if pending.revision_id > bundle.revision_id:
                        bundle = pending
                        restored_pending_target = True
                    elif (
                        pending.revision_id == bundle.revision_id
                        and pending.target_revision != bundle.target_revision
                    ):
                        raise RuntimeError(
                            "same revision has conflicting persisted closures"
                        ) from exc
                if bundle.configuration.service_id != bootstrap.service_id:
                    raise RuntimeError("saved Configuration service_id mismatch") from exc
                if bundle.deployment_id != bootstrap.deployment_id:
                    raise RuntimeError("saved Configuration deployment_id mismatch") from exc
                restored_from_snapshot = True
                _LOG.warning(
                    "using verified previous Configuration revision %d while unavailable",
                    bundle.revision_id,
                )
            await asyncio.to_thread(
                archive.set_max_bytes, bundle.configuration.configuration.cache_max_bytes
            )
            if not bundle.point_table_versions:
                raise RuntimeError(
                    "Storage cannot consume until the Service bundle pins PointTable versions"
                )
            # Validate current plus historical semantic bindings before
            # promoting the candidate into persistent SQLite and snapshot state.
            point_tables = ConfigurationPointTableResolver(
                fetcher=configuration,
                archive=archive,
                point_table_versions=bundle.point_table_versions,
                business_points_version=bundle.business_points_pin,
                historical_pins=metadata.list_semantic_pins(),
                device_bindings=(
                    *metadata.list_device_bindings(),
                    *bundle.device_bindings,
                ),
                persist_historical=lambda pins, business, binding: (
                    metadata.register_semantic_pins(
                        point_table_versions=pins,
                        business_points_pin=business,
                        device_bindings=(binding,),
                    )
                ),
            )
            metadata.register_semantic_pins(
                point_table_versions=bundle.point_table_versions,
                business_points_pin=bundle.business_points_pin,
                device_bindings=bundle.device_bindings,
            )
            if not restored_from_snapshot or restored_pending_target:
                # Restart activation of a staged target must be durable even
                # when Configuration is temporarily unreachable.
                await asyncio.to_thread(snapshot.store, bundle)
            metrics = StorageRuntimeMetrics()
            storage = TDengineBackend(config=bundle.configuration, metadata=metadata)
            await storage.open()
            try:
                ingestion = WideIngestionService(
                    storage=storage,
                    resolver=point_tables,
                    evidence=metadata,
                    metrics=metrics,
                    allowed_wind_farms=frozenset(bundle.configuration.allowed_wind_farms),
                    type_mapping=bundle.configuration.tdengine.type_mapping,
                    max_batch_rows=bundle.configuration.tdengine.ingestion.batch_max_rows,
                    max_batch_bytes=bundle.configuration.tdengine.ingestion.batch_max_bytes,
                )
                consumer = KafkaStorageConsumer(
                    configuration=bundle.configuration,
                    bootstrap_servers=bundle.configuration.kafka.bootstrap_servers,
                    ingestion=ingestion,
                    metadata=metadata,
                    metrics=metrics,
                )
                cursor_secret = os.getenv("WIND_HUB_STORAGE_CURSOR_SECRET", "").encode()
                if len(cursor_secret) < 32:
                    raise RuntimeError("WIND_HUB_STORAGE_CURSOR_SECRET must contain 32+ bytes")
                history = HistoryQueryService(
                    backend=storage,
                    codec=CursorCodec(cursor_secret),
                    max_page_size=bundle.configuration.query.max_page_size,
                    max_range_days=bundle.configuration.query.max_range_days,
                    max_concurrent=bundle.configuration.query.max_concurrent,
                    timeout_ms=bundle.configuration.query.timeout_ms,
                    read_available=lambda: not consumer.recovering,
                )
                wide_history = WideHistoryQueryService(
                    backend=storage,
                    codec=WideCursorCodec(cursor_secret),
                    max_page_size=bundle.configuration.query.max_page_size,
                    max_range_days=bundle.configuration.query.max_range_days,
                    max_concurrent=bundle.configuration.query.max_concurrent,
                    timeout_ms=bundle.configuration.query.timeout_ms,
                    read_available=lambda: not consumer.recovering,
                )
                jobs = AsyncStorageTasks(
                    config=bundle.configuration,
                    metadata=metadata,
                    history=history,
                    wide_history=wide_history,
                    units=point_tables,
                    backup=NativeTDengineBackup(
                        config=bundle.configuration,
                        metadata=metadata,
                    ),
                )
                watcher = StorageConfigurationWatcher(
                    client=configuration,
                    resolver=point_tables,
                    bundle=bundle,
                    service_id=bootstrap.service_id,
                    deployment_id=bootstrap.deployment_id,
                    servers=bootstrap.nats.servers,
                    metadata=metadata,
                    snapshot=snapshot,
                )
                point_tables.set_refresh_hook(watcher.refresh_now)
                if restored_from_snapshot:
                    watcher.status = "CONFIG_RECONCILE_DEGRADED"
                server = build_storage_grpc_server(
                    config=bundle.configuration,
                    metadata=metadata,
                    query=history,
                    wide_query=wide_history,
                    jobs=jobs,
                    point_tables=point_tables,
                    consumer=consumer,
                    cursor_secret=cursor_secret,
                    config_status=lambda: watcher.status,
                    config_revisions=lambda: (watcher.target_revision, watcher.active_revision),
                    metrics=metrics,
                )
                await consumer.start()
                try:
                    await server.start()
                except BaseException:
                    await consumer.stop()
                    raise
                watching = asyncio.create_task(
                    watcher.run(stop_event), name="storage-configuration-watch"
                )
                consume = asyncio.create_task(consumer.run(), name="storage-kafka-ingest")
                stopping = asyncio.create_task(stop_event.wait(), name="storage-shutdown-event")
                try:
                    finished, _ = await asyncio.wait(
                        {consume, watching, stopping},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if consume in finished:
                        # A failed writer must never leave an apparently healthy server.
                        await consume
                    if watching in finished:
                        await watching
                finally:
                    stop_event.set()
                    consumer.request_stop()
                    await server.stop(grace=3)
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
                    try:
                        await asyncio.wait_for(consume, timeout=10)
                    except TimeoutError:
                        consume.cancel()
                        await asyncio.gather(consume, return_exceptions=True)
                        _LOG.error("Storage Kafka worker exceeded shutdown deadline")
                    finally:
                        watching.cancel()
                        await asyncio.gather(watching, return_exceptions=True)
                        await jobs.close()
            finally:
                await storage.close()
        finally:
            await configuration.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Storage Service v1")
    parser.add_argument(
        "--bootstrap",
        type=Path,
        required=True,
        help="local minimal bootstrap YAML (not a duplicate business config)",
    )
    parser.add_argument("--check-bootstrap", action="store_true")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        bootstrap = load_bootstrap(arguments.bootstrap)
        if arguments.check_bootstrap:
            return 0
        asyncio.run(run_service(bootstrap))
    except Exception as exc:
        # TaskGroup propagates ExceptionGroup, which is still a normal
        # Exception. A failed worker must never leave the process healthy.
        _LOG.error(
            "Storage startup or runtime blocked: %s",
            type(exc).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
