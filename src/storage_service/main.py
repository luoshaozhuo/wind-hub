"""Storage Service process lifecycle: Configuration-pinned reliable Kafka ingestion."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from storage_service.application.ingestion import IngestionService
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
            bundle = await configuration.fetch_service_bundle(
                service_id=bootstrap.service_id
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
            storage = TDengineBackend(config=bundle.configuration, metadata=metadata)
            await storage.open()
            try:
                ingestion = IngestionService(
                    storage=storage,
                    point_tables=point_tables,
                    evidence=metadata,
                    max_batch_records=bundle.configuration.tdengine.batch_max_records,
                    max_batch_bytes=bundle.configuration.tdengine.batch_max_bytes,
                )
                consumer = KafkaStorageConsumer(
                    configuration=bundle.configuration,
                    bootstrap_servers=bundle.configuration.kafka.bootstrap_servers,
                    ingestion=ingestion,
                    metadata=metadata,
                )
                cursor_secret = os.getenv("WIND_HUB_STORAGE_CURSOR_SECRET", "").encode()
                if len(cursor_secret) < 32:
                    raise RuntimeError(
                        "WIND_HUB_STORAGE_CURSOR_SECRET must contain 32+ bytes"
                    )
                history = HistoryQueryService(
                    backend=storage, codec=CursorCodec(cursor_secret),
                    max_page_size=bundle.configuration.query.max_page_size,
                    max_range_days=bundle.configuration.query.max_range_days,
                    max_concurrent=bundle.configuration.query.max_concurrent,
                    timeout_ms=bundle.configuration.query.timeout_ms,
                )
                jobs = AsyncStorageTasks(
                    config=bundle.configuration, metadata=metadata, history=history,
                    backup=NativeTDengineBackup(
                        config=bundle.configuration, metadata=metadata,
                    ),
                )
                watcher = StorageConfigurationWatcher(
                    client=configuration, resolver=point_tables,
                    bundle=bundle, service_id=bootstrap.service_id,
                    servers=bootstrap.nats.servers, metadata=metadata,
                )
                server = build_storage_grpc_server(
                    config=bundle.configuration,
                    metadata=metadata, query=history, jobs=jobs,
                    point_tables=point_tables, consumer=consumer,
                    cursor_secret=cursor_secret,
                    config_status=lambda: watcher.status,
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
                consume = asyncio.create_task(
                    consumer.run(), name="storage-kafka-ingest"
                )
                stopping = asyncio.create_task(
                    stop_event.wait(), name="storage-shutdown-event"
                )
                try:
                    finished, _ = await asyncio.wait(
                        {consume, stopping}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if consume in finished:
                        # A failed worker cannot be hidden behind an idle process.
                        await consume
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
        "--bootstrap", type=Path, required=True,
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
    except (OSError, ValueError, RuntimeError) as exc:
        _LOG.error("Storage startup or runtime blocked: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
