"""NATS Core revision hints plus authoritative periodic Configuration reconciliation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress

import nats
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg

from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
    StorageBundle,
)
from storage_service.infrastructure.adapters.outbound.configuration_snapshot import (
    StorageBundleSnapshot,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore

_LOG = logging.getLogger(__name__)
_CONFIG_CHANGED_SUBJECT = "wh.config.changed"


class StorageConfigurationWatcher:
    """A failed/missed broadcast cannot prevent periodic revision convergence."""

    def __init__(
        self,
        *,
        client: ConfigurationGrpcClient,
        resolver: ConfigurationPointTableResolver,
        bundle: StorageBundle,
        service_id: str,
        deployment_id: str,
        servers: Sequence[str],
        metadata: MetadataStore,
        snapshot: StorageBundleSnapshot,
    ) -> None:
        if not servers:
            raise ValueError("NATS Core servers must be explicitly configured")
        self._client = client
        self._resolver = resolver
        self._active = bundle
        self._service_id = service_id
        self._deployment_id = deployment_id
        self.target_revision = bundle.target_revision
        self.active_revision = bundle.target_revision
        self.repository_revision_id = bundle.revision_id
        self.last_reconcile_error = ""
        self._servers = list(servers)
        self._metadata = metadata
        self._snapshot = snapshot
        self._changed = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_generation = 0
        self._reconcile_error: BaseException | None = None
        self.status = "READY"

    async def refresh_now(self) -> None:
        """Coalesce concurrent hints and unknown-ID requests into one authoritative pull."""
        generation = self._reconcile_generation
        async with self._reconcile_lock:
            if generation != self._reconcile_generation:
                if self._reconcile_error is not None:
                    raise self._reconcile_error
                return
            try:
                await self._reconcile()
            except ConfigHistoryUnavailableError as exc:
                self.status = "CONFIG_HISTORY_UNAVAILABLE"
                self.last_reconcile_error = type(exc).__name__
                self._reconcile_error = exc
                raise
            except Exception as exc:
                self.status = "CONFIG_RECONCILE_DEGRADED"
                self.last_reconcile_error = type(exc).__name__
                self._reconcile_error = exc
                raise
            except BaseException as exc:
                self._reconcile_error = exc
                raise
            else:
                self._reconcile_error = None
                self.last_reconcile_error = ""
            finally:
                self._reconcile_generation += 1

    async def _reconcile(self) -> None:
        revision = await self._client.get_current_revision()
        if revision < self.repository_revision_id:
            raise ConfigHistoryUnavailableError("Configuration publication cursor moved backwards")
        if revision == self.repository_revision_id and self.target_revision == self.active_revision:
            self.status = "READY"
            return
        updated = await self._client.fetch_service_bundle(
            deployment_id=self._deployment_id, service_id=self._service_id
        )
        if updated.deployment_id != self._deployment_id:
            raise ConfigHistoryUnavailableError("Deployment identity changed in response")
        if updated.revision_id < self.repository_revision_id:
            raise ConfigHistoryUnavailableError("Configuration authority revision moved backwards")
        if (
            updated.revision_id == self.repository_revision_id
            and updated.target_revision != self.target_revision
        ):
            raise ConfigHistoryUnavailableError(
                "Configuration authority changed an immutable repository revision"
            )
        self.repository_revision_id = updated.revision_id
        self.target_revision = updated.target_revision
        if updated.target_revision == self.active_revision:
            # A Repository publication unrelated to this Service changes only
            # the global cursor. No disk writes or runtime replacement occur.
            self.status = "READY"
            return

        old = self._active.configuration.model_dump(exclude={"config_sources", "file_version"})
        new = updated.configuration.model_dump(exclude={"config_sources", "file_version"})
        if old != new:
            self.status = "RESTART_REQUIRED"
            _LOG.warning("Storage target requires controlled restart")
            return

        self._resolver.validate_immutable_pins(
            point_table_versions=updated.point_table_versions,
            business_points_pin=updated.business_points_pin,
            device_bindings=updated.device_bindings,
        )
        await asyncio.to_thread(
            self._metadata.register_semantic_pins,
            point_table_versions=updated.point_table_versions,
            business_points_pin=updated.business_points_pin,
        )
        await asyncio.to_thread(self._snapshot.store, updated)
        self._resolver.add_immutable_pins(
            point_table_versions=updated.point_table_versions,
            business_points_pin=updated.business_points_pin,
            device_bindings=updated.device_bindings,
        )
        self._active = updated
        self.active_revision = updated.target_revision
        self.last_reconcile_error = ""
        self.status = "READY"
        _LOG.info("Storage accepted effective closure %s", self.active_revision)

    async def run(self, stop: asyncio.Event) -> None:
        connection: NatsClient | None = None
        timeout = self._active.configuration.configuration.reconcile_interval_s
        while not stop.is_set():
            if connection is None:
                try:
                    connection = await nats.connect(
                        servers=self._servers,
                        connect_timeout=3,
                        max_reconnect_attempts=-1,
                    )

                    async def notify(message: Msg) -> None:
                        # NATS hints are not authoritative revisions; a bounded
                        # message only triggers a full gRPC revision check.
                        if len(message.data) <= 4096:
                            self._changed.set()

                    await connection.subscribe(
                        _CONFIG_CHANGED_SUBJECT,
                        cb=notify,
                    )
                except Exception:
                    _LOG.warning(
                        "Storage NATS broadcast unavailable; periodic polling remains active"
                    )
                    if connection is not None:
                        await connection.close()
                    connection = None
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            self._changed.clear()
            if stop.is_set():
                break
            try:
                await self.refresh_now()
            except ConfigHistoryUnavailableError:
                _LOG.warning("Storage rejected an incomplete immutable configuration revision")
            except Exception:
                _LOG.warning("Storage Configuration reconciliation unavailable")
        if connection is not None:
            await connection.close()
