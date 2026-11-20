"""Storage Service process lifecycle: Configuration-pinned reliable Kafka ingestion."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

import grpc

from storage_service.application.ingestion import IngestionService
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.query import CursorCodec, HistoryQueryService
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
            try:
                bundle = await configuration.fetch_service_bundle(service_id=bootstrap.service_id)
                await asyncio.to_thread(snapshot.store, bundle)
            except (grpc.aio.AioRpcError, TimeoutError, ConnectionError) as exc:
                if isinstance(exc, grpc.aio.AioRpcError) and exc.code() not in (
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                ):
                    raise
                bundle = await asyncio.to_thread(snapshot.load)
                if bundle.configuration.service_id != bootstrap.service_id:
                    raise RuntimeError("saved Configuration service_id mismatch") from exc
                restored_from_snapshot = True
                _LOG.warning(
                    "using verified previous Configuration revision %d while unavailable",
                    bundle.revision_id,
                )
            if not bundle.point_table_versions:
                raise RuntimeError(
                    "Storage cannot consume until the Service bundle pins PointTable versions"
                )
            # Persist verified closure identities before consuming any Kafka
            # data; historical mappings survive all later service restarts.
            metadata.register_semantic_pins(
                point_table_versions=bundle.point_table_versions,
                business_points_pin=bundle.business_points_pin,
            )
            point_tables = ConfigurationPointTableResolver(
                fetcher=configuration,
                archive=archive,
                point_table_versions=bundle.point_table_versions,
                business_points_version=bundle.business_points_pin,
                historical_pins=metadata.list_semantic_pins(),
            )
            metrics = StorageRuntimeMetrics()
            storage = TDengineBackend(config=bundle.configuration, metadata=metadata)
            await storage.open()
            try:
                ingestion = IngestionService(
                    storage=storage,
                    point_tables=point_tables,
                    evidence=metadata,
                    metrics=metrics,
                    allowed_wind_farms=frozenset(bundle.configuration.allowed_wind_farms),
                    max_batch_records=bundle.configuration.tdengine.batch_max_records,
                    max_batch_bytes=bundle.configuration.tdengine.batch_max_bytes,
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
                )
                jobs = AsyncStorageTasks(
                    config=bundle.configuration,
                    metadata=metadata,
                    history=history,
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
                    servers=bootstrap.nats.servers,
                    metadata=metadata,
                    snapshot=snapshot,
                )
                if restored_from_snapshot:
                    watcher.status = "CONFIG_RECONCILE_DEGRADED"
                server = build_storage_grpc_server(
                    config=bundle.configuration,
                    metadata=metadata,
                    query=history,
                    jobs=jobs,
                    point_tables=point_tables,
                    consumer=consumer,
                    cursor_secret=cursor_secret,
                    config_status=lambda: watcher.status,
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
