"""Atomic shared configuration revisions and per-device authorization policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
    SharedDocuments,
)
from core.infrastructure.config.shared_snapshot import build_shared_snapshot

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


def _writable(document: Any, point_id: str) -> bool:
    """Conservative, source-level write permission; deny unknown point forms."""
    table = document.templates.point_table.document
    points = table.get("points", {})
    if not isinstance(points, dict):
        return False
    point = points.get(point_id)
    if not isinstance(point, dict):
        return False
    # Inherited or encoded permission is resolved by the real typed snapshot;
    # raw declarations alone are never treated as permission.
    access = point.get("access")
    return access in ("write", "read_write", "rw")


class CommanderConfigManager:
    """Validate candidates before change; never expose a partial revision."""

    def __init__(
        self,
        root: Path,
        service_config: Path,
        *,
        quiesce: Callable[[frozenset[str]], object],
    ) -> None:
        self._root = root
        self._service_config = service_config
        self._quiesce = quiesce
        self._lock = asyncio.Lock()
        self._documents: SharedDocuments | None = None
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
        if documents is None or spec.device_id in self._disabled:
            return False
        definition = documents.devices.get(spec.device_id)
        if definition is None or not definition.enabled:
            return False
        expected = self._fingerprints.get(spec.device_id)
        return (
            expected is not None
            and spec.config_reference == expected
            and _writable(definition, spec.point_id)
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
            changed = frozenset(
                key
                for key in self._fingerprints.keys() | new_fp.keys()
                if self._fingerprints.get(key) != new_fp.get(key)
            )
            result = self._quiesce(changed)
            if asyncio.iscoroutine(result):
                blocked = frozenset(await result)
            else:
                blocked = frozenset(result)
            retained = dict(self._documents.devices)
            current_fp = dict(self._fingerprints)
            for device_id in changed - blocked:
                if device_id in candidate.devices:
                    retained[device_id] = candidate.devices[device_id]
                    current_fp[device_id] = new_fp[device_id]
                else:
                    retained.pop(device_id, None)
                    current_fp.pop(device_id, None)
            self._documents = SharedDocuments(
                site=candidate.site,
                devices=retained,
                units=candidate.units,
                business_points=candidate.business_points,
                revision=candidate.revision,
            )
            self._fingerprints = current_fp
            self._disabled = set(blocked)
            return ReloadReport(
                revision=self.revision,
                changed=tuple(sorted(changed - blocked)),
                unchanged=tuple(sorted(set(candidate.devices) - changed)),
                blocked=tuple(sorted(blocked)),
                restart_required=restart_required,
            )
