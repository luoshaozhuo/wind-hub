"""Atomic shared configuration revisions and per-device authorization policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from commander.application.settings import ServiceSettings
from commander.domain.command import CommandSpec
from configuration.effective import resolve_effective_device
from configuration.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
    SharedDocuments,
)
from configuration.shared_snapshot import (
    SharedDomainSnapshot,
    build_shared_snapshot,
)
from core.application.port import ProtocolPort
from core.application.protocol_contract import ProtocolWrite
from core.application.protocol_registry import ProtocolRegistry
from core.domain import DeviceId, DeviceModelId, PointAccess, PointTableId, convert_value
from core.domain.point import raw_write_value


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
            (r.reference.template_id, r.reference.file_version, r.content_hash) for r in refs
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _writable(snapshot: SharedDomainSnapshot, device_id: str, point_id: str, value: object) -> bool:
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


def _validate_unique_connections(snapshot: SharedDomainSnapshot) -> None:
    """Reject two controlled devices sharing one physical connection identity."""
    seen: dict[tuple[str, str, int | None, str], str] = {}
    for device_id, device in snapshot.site.devices.items():
        if not device.enabled:
            continue
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        # Identity must reflect effective model/device option merge and port
        # fallback; otherwise two devices that physically coincide can be
        # admitted (or two distinct targets rejected on a raw-config collision).
        effective_device, effective_options = resolve_effective_device(
            device, model, table.protocol.name
        )
        options = json.dumps(
            dict(effective_options),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        identity = (
            table.protocol.name,
            effective_device.endpoint.host,
            effective_device.endpoint.port,
            options,
        )
        prior = seen.get(identity)
        if prior is not None:
            raise ValueError(f"duplicate protocol connection for {prior} and {device_id}")
        seen[identity] = str(device_id)


class SettingsLoader(Protocol):
    def __call__(self) -> ServiceSettings: ...


class CommanderConfigManager:
    """Validate candidates before change; never expose a partial revision."""

    def __init__(
        self,
        *,
        quiesce: Callable[[frozenset[str]], Awaitable[frozenset[str]]],
        settings_loader: SettingsLoader,
    ) -> None:
        self._settings_loader = settings_loader
        self._quiesce = quiesce
        self._lock = asyncio.Lock()
        self._documents: SharedDocuments | None = None
        self._snapshot: SharedDomainSnapshot | None = None
        self._settings: ServiceSettings | None = None
        self._fingerprints: dict[str, str] = {}
        self._disabled: set[str] = set()

    def _load(self, settings: ServiceSettings) -> SharedDocuments:
        paths = SharedConfigPaths(
            instances=settings.devices_path.parent,
            templates=settings.templates_root,
            devices_file=settings.devices_path,
            load_site=False,
            coordination_root=settings.coordination_root,
        )
        docs = SharedDocumentLoader(paths).load()
        build_shared_snapshot(docs)
        # Refuse a candidate read across concurrent file replacement.
        verify = SharedDocumentLoader(paths).load()
        if verify.revision != docs.revision:
            raise RuntimeError("shared configuration changed during validation")
        return docs

    def start(self) -> ServiceSettings:
        settings = self._settings_loader()
        docs = self._load(settings)
        self._settings = settings
        self._documents = docs
        self._snapshot = build_shared_snapshot(docs)
        _validate_unique_connections(self._snapshot)
        self._fingerprints = {
            device: _device_fingerprint(definition) for device, definition in docs.devices.items()
        }
        return settings

    @property
    def active_settings(self) -> ServiceSettings:
        if self._settings is None:
            raise RuntimeError("Commander service settings are not initialized")
        return self._settings

    @property
    def snapshot(self) -> SharedDomainSnapshot:
        if self._snapshot is None:
            raise RuntimeError("Commander configuration is not initialized")
        return self._snapshot

    @property
    def documents(self) -> SharedDocuments:
        if self._documents is None:
            raise RuntimeError("Commander configuration is not initialized")
        return self._documents

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

    def prepare_write(self, spec: CommandSpec) -> ProtocolWrite:
        """Convert authorized engineering input to protocol raw value."""
        if not self.authorized(spec) or self._snapshot is None:
            raise PermissionError("device, point or config version is not writable")
        device = self._snapshot.site.devices[DeviceId(spec.device_id)]
        model = self._snapshot.device_models[device.device_model_id]
        point = self._snapshot.point_tables[model.point_table_id].point(spec.point_id)
        business = self._snapshot.business_points[point.business_point_id]
        typed = business.coerce(spec.value)
        point_value = typed
        if (
            isinstance(typed, int | float)
            and not isinstance(typed, bool)
            and business.standard_unit != point.source_unit
        ):
            if isinstance(typed, int) and abs(typed) > 2**53:
                raise ValueError(
                    "large integer cannot undergo floating-point unit conversion safely"
                )
            point_value = convert_value(float(typed), business.standard_unit, point.source_unit)
        raw_value = raw_write_value(point, point_value)
        if isinstance(raw_value, float) and not math.isfinite(raw_value):
            raise ValueError("scaled protocol write value must be finite")
        return ProtocolWrite(point=spec.point_id, value=raw_value)

    def create_session(self, device_id: str, registry: ProtocolRegistry) -> ProtocolPort:
        """Construct a disconnected Core protocol driver from active configuration."""
        snapshot = self._snapshot
        if snapshot is None or device_id in self._disabled:
            raise RuntimeError("device configuration is unavailable")
        device = snapshot.site.devices.get(DeviceId(device_id))
        if device is None or not device.enabled:
            raise KeyError(device_id)
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        effective_device, options = resolve_effective_device(device, model, table.protocol.name)
        return registry.create(effective_device.endpoint, table, options)

    def device_revision(self, device_id: str) -> str | None:
        return self._fingerprints.get(device_id)

    def blocked_devices(self) -> frozenset[str]:
        return frozenset(self._disabled)

    async def reload(self) -> ReloadReport:
        """Swap only safe devices; retain old binding for unsafe devices."""
        async with self._lock:
            if self._documents is None or self._settings is None:
                raise RuntimeError("config manager not started")
            service_candidate = self._settings_loader()
            candidate = self._load(self._settings)
            restart_required = service_candidate != self._settings
            new_fp = {name: _device_fingerprint(value) for name, value in candidate.devices.items()}
            # Compare source documents at the same representation and grain.
            shared_changed = (
                candidate.site != self._documents.site
                or candidate.units != self._documents.units
                or candidate.business_points != self._documents.business_points
            )
            changed = frozenset(
                key
                for key in self._fingerprints.keys() | new_fp.keys()
                if shared_changed or self._fingerprints.get(key) != new_fp.get(key)
            )
            blocked = frozenset(await self._quiesce(changed | self._disabled))
            # Shared site/units/business definitions cannot be applied partially.
            # Keep the old shared documents whenever a dependent device is unsafe.
            use_new_shared = not (shared_changed and blocked)
            effective_site = candidate.site if use_new_shared else self._documents.site
            effective_units = candidate.units if use_new_shared else self._documents.units
            effective_business = (
                candidate.business_points if use_new_shared else self._documents.business_points
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
            if not blocked:
                applied_revision = candidate.revision
            elif not applied_devices:
                applied_revision = self._documents.revision
            else:
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
                devices_file_version=(
                    candidate.devices_file_version
                    if not blocked
                    else self._documents.devices_file_version
                ),
            )
            effective_snapshot = build_shared_snapshot(applied)
            _validate_unique_connections(effective_snapshot)
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
