"""Configuration Service executable: token-authorized gRPC on trusted networks."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc

from configuration_service.application.metrics import RpcMetrics
from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import (
    ConfigurationApplication,
    RepositoryLimits,
)
from configuration_service.infrastructure.adapters.inbound.grpc.common_server import (
    CommonServicer,
)
from configuration_service.infrastructure.adapters.inbound.grpc.repository_server import (
    RepositoryServicer,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.backup import (
    SQLiteBackupManager,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from configuration_service.infrastructure.adapters.outbound.yaml.document_codec import (
    YamlDocumentCodec,
)
from configuration_service.settings import ServerSettings, load_settings
from contracts import common_pb2_grpc, configuration_repository_pb2_grpc

_LOG = logging.getLogger(__name__)


def serve(settings: ServerSettings, bootstrap: Path) -> None:
    """Run until process shutdown, releasing ownership only after gRPC stops."""
    repository = SQLiteRepository(
        settings.sqlite_path,
        application_version="0.1.0",
        busy_timeout_ms=settings.write_timeout_ms,
    )
    repository.initialize()
    worker_pool = ThreadPoolExecutor(
        max_workers=settings.max_workers, thread_name_prefix="configuration-rpc"
    )
    backup_pool = ThreadPoolExecutor(
        max_workers=settings.max_backup_jobs, thread_name_prefix="configuration-backup"
    )
    backups = SQLiteBackupManager(repository, settings.backup_root)
    server: grpc.Server | None = None
    try:
        interrupted = backups.recover()
        if interrupted:
            _LOG.warning("marked %s interrupted backup jobs", interrupted)
        application = ConfigurationApplication(
            repository,
            limits=RepositoryLimits(
                max_request_bytes=settings.max_request_bytes,
                max_bundle_bytes=settings.max_bundle_bytes,
                max_dependencies=settings.max_dependencies,
                max_documents_per_publication=settings.max_documents_per_publication,
                candidate_ttl_seconds=settings.candidate_ttl_seconds,
                max_candidates=settings.max_candidates,
                max_backup_jobs=settings.max_backup_jobs,
                page_size=settings.page_size,
            ),
            backups=backups,
            own_service_id=settings.service_id,
            codec=YamlDocumentCodec(),
        )
        for specification in iter_schemas():
            application.register_schema(
                config_type=specification.config_type,
                document_schema_version=specification.schema_version,
                fields=specification.fields,
                header=specification.header,
            )
        cursor_secret = settings.cursor_secret_file.read_bytes()
        scheduled_backups: set[str] = set()
        scheduled_lock = threading.Lock()

        def run_backup(task_id: str) -> None:
            try:
                application.run_backup(task_id)
            except Exception:
                _LOG.exception("Configuration backup task %s failed", task_id)
            finally:
                with scheduled_lock:
                    scheduled_backups.discard(task_id)

        def schedule_backup(task_id: str) -> None:
            with scheduled_lock:
                if task_id in scheduled_backups:
                    return
                scheduled_backups.add(task_id)
            try:
                backup_pool.submit(run_backup, task_id)
            except Exception:
                with scheduled_lock:
                    scheduled_backups.discard(task_id)
                raise

        rpc_metrics = RpcMetrics()
        repository_servicer = RepositoryServicer(
            application,
            permissions=settings.permissions,
            token_sources=settings.token_sources,
            cursor_secret=cursor_secret,
            submit_backup=schedule_backup,
            metrics=rpc_metrics,
        )
        common_servicer = CommonServicer(
            application, repository_servicer, settings, bootstrap, rpc_metrics
        )
        # YAML limits govern the sum of YAML bytes; serialized protobuf
        # responses also carry version metadata, file identities and hashes.
        # Reserve transport framing headroom without relaxing YAML quotas.
        protobuf_headroom = max(
            1024 * 1024,
            min(settings.max_dependencies * 4096, 64 * 1024 * 1024),
        )
        server = grpc.server(
            worker_pool,
            maximum_concurrent_rpcs=settings.max_concurrent_queries,
            options=[
                ("grpc.max_receive_message_length", settings.max_request_bytes),
                (
                    "grpc.max_send_message_length",
                    settings.max_bundle_bytes + protobuf_headroom,
                ),
            ],
        )
        configuration_repository_pb2_grpc.add_ConfigurationRepositoryServiceServicer_to_server(
            repository_servicer, server
        )
        common_pb2_grpc.add_CommonServiceServicer_to_server(common_servicer, server)
        bind = f"{settings.host}:{settings.port}"
        if server.add_insecure_port(bind) == 0:
            raise RuntimeError("failed to bind Configuration Service endpoint")
        server.start()
        _LOG.info("Configuration Service listening at %s", bind)
        should_stop = False

        def stop_signal(_signal: int, _frame: object) -> None:
            nonlocal should_stop
            should_stop = True

        signal.signal(signal.SIGTERM, stop_signal)
        signal.signal(signal.SIGINT, stop_signal)
        while not should_stop:
            signal.pause()
    finally:
        if server is not None:
            server.stop(grace=5).wait(timeout=10)
        backup_pool.shutdown(wait=True, cancel_futures=False)
        worker_pool.shutdown(wait=True, cancel_futures=True)
        repository.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wind-hub Configuration Service")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        path = args.config.resolve(strict=True)
        serve(load_settings(path), path)
    except (OSError, ValueError, RuntimeError) as exc:
        _LOG.error("Configuration Service startup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
