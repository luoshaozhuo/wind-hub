"""Atomic shared configuration revisions and per-device authorization policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
    SharedDocuments,
)
from core.domain import DeviceId, DeviceModelId, PointAccess, PointTableId
from core.infrastructure.config.shared_snapshot import (
    SharedDomainSnapshot,
    build_shared_snapshot,
)

from commander.domain.command import CommandSpec
from commander.infrastructure.service_settings import (
    ServiceSettings,
    load_service_settings,
)


@dataclass(frozen=True, slots=True)
class ReloadReport:
    revision: str
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    blocked: tuple[str, ...]
    restart_required: bool


def _device_fingerprint(document: Any) -> str:
    """Ignore checkout paths; include actual template ancestry and overrides."""
    template = document.templates
    refs = (template.model, template.protocol, template.point_table, *template.inherited_tables)
    payload = {
        "enabled": document.enabled,
        "name": document.name,
        "endpoint": document.endpoint,
        "options": document.protocol_options,
        "templates": [
            (r.reference.template_id, r.reference.file_version, r.content_hash)
            for r in refs
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _writable(
    snapshot: SharedDomainSnapshot, device_id: str, point_id: str, value: object
) -> bool:
    device = snapshot.site.devices.get(DeviceId(device_id))
    if device is None or not device.enabled:
        return False
    model = snapshot.device_models.get(DeviceModelId(device.device_model_id))
    if model is None:
        return False
    table = snapshot.point_tables.get(PointTableId(model.point_table_id))
    if table is None:
        return False
    point = table.points.get(point_id)
    if point is None or point.access not in (PointAccess.WRITE, PointAccess.READ_WRITE):
        return False
    business = snapshot.business_points.get(point.business_point_id)
    if business is None:
        return False
    try:
        business.coerce(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


class CommanderConfigManager:
    """Validate candidates before change; never expose a partial revision."""

    def __init__(
        self,
        root: Path,
        service_config: Path,
        *,
        quiesce: Callable[[frozenset[str]], Awaitable[frozenset[str]]],
    ) -> None:
        self._root = root
        self._service_config = service_config
        self._quiesce = quiesce
        self._lock = asyncio.Lock()
        self._documents: SharedDocuments | None = None
        self._snapshot: SharedDomainSnapshot | None = None
        self._settings: ServiceSettings | None = None
        self._fingerprints: dict[str, str] = {}
        self._disabled: set[str] = set()

    def _load(self) -> SharedDocuments:
        docs = SharedDocumentLoader(
            SharedConfigPaths(
                instances=self._root / "instances",
                templates=self._root / "templates",
            )
        ).load()
        build_shared_snapshot(docs)
        return docs

    def start(self) -> ServiceSettings:
        settings = load_service_settings(self._service_config)
        docs = self._load()
        self._settings = settings
        self._documents = docs
        self._snapshot = build_shared_snapshot(docs)
        self._fingerprints = {
            device: _device_fingerprint(definition)
            for device, definition in docs.devices.items()
        }
        return settings

    @property
    def revision(self) -> str:
        return self._documents.revision if self._documents is not None else ""

    def authorized(self, spec: CommandSpec) -> bool:
        documents = self._documents
        if documents is None or self._snapshot is None or spec.device_id in self._disabled:
            return False
        definition = documents.devices.get(spec.device_id)
        if definition is None or not definition.enabled:
            return False
        expected = self._fingerprints.get(spec.device_id)
        return (
            expected is not None
            and spec.config_reference == expected
            and _writable(self._snapshot, spec.device_id, spec.point_id, spec.value)
        )

    def device_revision(self, device_id: str) -> str | None:
        return self._fingerprints.get(device_id)

    async def reload(self) -> ReloadReport:
        """Swap only safe devices; retain old binding for unsafe devices."""
        async with self._lock:
            if self._documents is None or self._settings is None:
                raise RuntimeError("config manager not started")
            candidate = self._load()
            service_candidate = load_service_settings(self._service_config)
            restart_required = service_candidate != self._settings
            new_fp = {
                name: _device_fingerprint(value)
                for name, value in candidate.devices.items()
            }
            shared_changed = (
                candidate.site != self._documents.site
                or candidate.units != self._documents.units
                or candidate.business_points != self._documents.business_points
            )
            changed = frozenset(
                key
                for key in self._fingerprints.keys() | new_fp.keys()
                if shared_changed
                or self._fingerprints.get(key) != new_fp.get(key)
            )
            blocked = frozenset(await self._quiesce(changed))
            # Shared site/units/business definitions cannot be applied partially.
            # Keep the old shared documents whenever a dependent device is unsafe.
            use_new_shared = not (shared_changed and blocked)
            effective_site = candidate.site if use_new_shared else self._documents.site
            effective_units = candidate.units if use_new_shared else self._documents.units
            effective_business = (
                candidate.business_points
                if use_new_shared
                else self._documents.business_points
            )
            retained = dict(self._documents.devices)
            current_fp = dict(self._fingerprints)
            applied_devices = changed - blocked
            if shared_changed and blocked:
                applied_devices = frozenset()
                blocked = changed
            for device_id in applied_devices:
                if device_id in candidate.devices:
                    retained[device_id] = candidate.devices[device_id]
                    current_fp[device_id] = new_fp[device_id]
                else:
                    retained.pop(device_id, None)
                    current_fp.pop(device_id, None)
            applied_revision = hashlib.sha256(
                json.dumps(
                    {
                        "site": effective_site,
                        "units": effective_units,
                        "business_points": effective_business,
                        "devices": current_fp,
                    },
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            applied = SharedDocuments(
                site=effective_site,
                devices=retained,
                units=effective_units,
                business_points=effective_business,
                revision=applied_revision,
            )
            effective_snapshot = build_shared_snapshot(applied)
            self._documents = applied
            self._snapshot = effective_snapshot
            self._fingerprints = current_fp
            self._disabled = set(blocked)
            return ReloadReport(
                revision=self.revision,
                changed=tuple(sorted(applied_devices)),
                unchanged=tuple(sorted(set(candidate.devices) - changed)),
                blocked=tuple(sorted(blocked)),
                restart_required=restart_required,
            )
