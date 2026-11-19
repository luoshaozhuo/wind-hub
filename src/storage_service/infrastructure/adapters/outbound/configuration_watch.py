"""NATS Core revision hints plus authoritative periodic Configuration reconciliation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import nats
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg

from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigHistoryUnavailable,
    ConfigurationPointTableResolver,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
    StorageBundle,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore

_LOG = logging.getLogger(__name__)
_CONFIG_CHANGED_SUBJECT = "wh.config.changed"


class StorageConfigurationWatcher:
    """A failed/missed broadcast cannot prevent periodic revision convergence."""

    def __init__(
        self, *,
        client: ConfigurationGrpcClient,
        resolver: ConfigurationPointTableResolver,
        bundle: StorageBundle,
        service_id: str,
        servers: Sequence[str], metadata: MetadataStore,
    ) -> None:
        if not servers:
            raise ValueError("NATS Core servers must be explicitly configured")
        self._client = client
        self._resolver = resolver
        self._active = bundle
        self._service_id = service_id
        self._servers = list(servers)
        self._metadata = metadata
        self._changed = asyncio.Event()
        self.status = "READY"

    async def _reconcile(self) -> None:
        updated = await self._client.fetch_service_bundle(service_id=self._service_id)
        if updated.revision_id <= self._active.revision_id:
            return
        # Changing brokers, DB credentials, retention, security or export limits
        # requires a controlled restart. No hot reconfiguration of in-flight IO.
        old = self._active.configuration.model_dump(exclude={"config_sources"})
        new = updated.configuration.model_dump(exclude={"config_sources"})
        if old != new:
            self.status = "RESTART_REQUIRED"
            _LOG.warning(
                "Storage config changed outside immutable reference set; "
                "continue on verified revision %d until controlled restart",
                self._active.revision_id,
            )
            return
        # SQLite commit precedes any in-memory generation switch.
        await asyncio.to_thread(
            self._metadata.register_semantic_pins,
            point_table_versions=updated.point_table_versions,
            business_points_pin=updated.business_points_pin,
        )
        self._resolver.add_immutable_pins(
            point_table_versions=updated.point_table_versions,
            business_points_pin=updated.business_points_pin,
        )
        self._active = updated
        self.status = "READY"
        _LOG.info("Storage accepted Configuration revision %d", updated.revision_id)

    async def run(self, stop: asyncio.Event) -> None:
        connection: NatsClient | None = None
        timeout = self._active.configuration.configuration.reconcile_interval_s
        while not stop.is_set():
            if connection is None:
                try:
                    connection = await nats.connect(
                        servers=self._servers, connect_timeout=3,
                        max_reconnect_attempts=-1,
                    )

                    async def notify(message: Msg) -> None:
                        # NATS hints are not authoritative revisions; a bounded
                        # message only triggers a full gRPC revision check.
                        if len(message.data) <= 4096:
                            self._changed.set()

                    await connection.subscribe(
                        _CONFIG_CHANGED_SUBJECT, cb=notify,
                    )
                except Exception:
                    _LOG.warning(
                        "Storage NATS broadcast unavailable; periodic polling remains active"
                    )
                    if connection is not None:
                        await connection.close()
                    connection = None
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
            self._changed.clear()
            if stop.is_set():
                break
            try:
                await self._reconcile()
            except ConfigHistoryUnavailable:
                self.status = "CONFIG_HISTORY_UNAVAILABLE"
                _LOG.warning("Storage rejected an incomplete immutable configuration revision")
            except Exception:
                self.status = "CONFIG_RECONCILE_DEGRADED"
                _LOG.warning("Storage Configuration reconciliation unavailable")
        if connection is not None:
            await connection.close()
