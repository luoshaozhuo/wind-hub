"""Storage Service process lifecycle: Configuration-pinned reliable Kafka ingestion."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from storage_service.application.ingestion import IngestionService
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
    ImmutableConfigArchive,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
)
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineBackend,
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
            point_tables = ConfigurationPointTableResolver(
                fetcher=configuration,
                archive=archive,
                point_table_versions=bundle.point_table_versions,
                business_points_version=bundle.business_points_pin,
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
                await consumer.start()
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
                    consumer.request_stop()
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
                    await consume
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
