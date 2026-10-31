"""Coordinate stored Collector configuration with active runtime state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from collector.application.config_service import CollectorConfigService
from core.application.errors import ConfigError

from .config_patches import ConfigPatch
from .transactions import (
    CollectorConfigTransactions,
    ConfigMutationResult,
    ConfigSelector,
    DeviceApplyState,
    RevisionConflictError,
)

logger = logging.getLogger(__name__)


class CollectorConfigAdmin:
    """Persist one document then converge runtime, recording any divergence."""

    def __init__(
        self,
        store: CollectorConfigTransactions,
        config_service: CollectorConfigService,
        *,
        authorize: Callable[[ConfigSelector, str], None],
    ) -> None:
        self._store = store
        self._config = config_service
        self._authorize = authorize
        self._lock = asyncio.Lock()
        self._device_errors: dict[str, str] = {}

    @property
    def runtime_status(self) -> str:
        """Aggregate cached config statuses, with failure taking precedence."""
        statuses = {item.status for item in self._store.cached_statuses()}
        if "APPLY_FAILED" in statuses or "STORED" in statuses:
            return "APPLY_FAILED"
        if "RESTART_REQUIRED" in statuses:
            return "RESTART_REQUIRED"
        return "APPLIED"

    def get(self, selector: ConfigSelector) -> tuple[dict[str, Any], str]:
        self._authorize(selector, "read")
        return self._store.read_redacted(selector)

    def validate(self, selector: ConfigSelector, document: dict[str, Any]) -> str:
        self._authorize(selector, "validate")
        return self._store.validate(selector, document)

    async def replace(
        self,
        selector: ConfigSelector,
        document: dict[str, Any],
        *,
        expected_revision: str,
        request_id: str,
    ) -> ConfigMutationResult:
        self._authorize(selector, "write")
        async with self._lock:
            stored = await self._store.replace(
                selector,
                document,
                expected_revision=expected_revision,
                request_id=request_id,
            )
            result = await self._apply(selector, stored)
            logger.info(
                "Collector config mutation scope=%s name=%s request_id=%s "
                "stored_revision=%s status=%s",
                selector.scope,
                selector.name,
                request_id,
                result.stored_revision,
                result.status,
            )
            return result

    async def update(
        self,
        selector: ConfigSelector,
        patches: tuple[ConfigPatch, ...],
        *,
        expected_revision: str,
        request_id: str,
    ) -> ConfigMutationResult:
        self._authorize(selector, "write")
        async with self._lock:
            stored = await self._store.update(
                selector,
                patches,
                expected_revision=expected_revision,
                request_id=request_id,
            )
            result = await self._apply(selector, stored)
            logger.info(
                "Collector config mutation scope=%s name=%s request_id=%s "
                "stored_revision=%s status=%s",
                selector.scope,
                selector.name,
                request_id,
                result.stored_revision,
                result.status,
            )
            return result

    async def set_task_desired(
        self,
        device_id: str,
        state: str,
        *,
        expected_revision: str,
        request_id: str,
    ) -> ConfigMutationResult:
        """Persist a device task intent with optimistic revision and request ID."""
        if state not in ("START", "STOP"):
            raise ConfigError("invalid desired state")
        selector = ConfigSelector("INSTANCE", "devices")
        self._authorize(selector, "write")
        raw, revision = self._store.read(selector)
        if revision != expected_revision:
            raise RevisionConflictError("configuration revision conflict")
        devices = raw.get("devices")
        if not isinstance(devices, dict) or device_id not in devices:
            raise KeyError(device_id)
        definition = devices[device_id]
        if not isinstance(definition, dict) or definition.get("enabled", True) is False:
            raise ConfigError("target device is disabled")
        overrides = definition.setdefault("overrides", {})
        if not isinstance(overrides, dict):
            raise ConfigError("invalid instance overrides")
        task = overrides.setdefault("task", {})
        if not isinstance(task, dict):
            raise ConfigError("invalid task override")
        task["desired_state"] = state
        return await self.replace(
            selector,
            raw,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    async def _apply(
        self, selector: ConfigSelector, stored: ConfigMutationResult
    ) -> ConfigMutationResult:
        if stored.status == "RESTART_REQUIRED":
            return stored
        previous_intent = dict(self._config.current_config.desired_states)
        result = await self._config.reload()
        changed_intent: set[str] = set()
        if selector.scope == "INSTANCE" and selector.name == "devices":
            current_doc, _ = self._store.read(selector)
            for device_id, item in current_doc.get("devices", {}).items():
                overrides = item.get("overrides", {}) if isinstance(item, dict) else {}
                task = overrides.get("task", {}) if isinstance(overrides, dict) else {}
                new_state = task.get("desired_state") if isinstance(task, dict) else None
                if new_state is not None and new_state != previous_intent.get(device_id):
                    changed_intent.add(device_id)
        affected = sorted(
            set(
                result.diff.devices.added
                + result.diff.devices.updated
                + result.diff.devices.removed
                + result.diff.tasks.added
                + result.diff.tasks.updated
                + result.diff.tasks.removed
                + list(changed_intent)
            )
        )
        by_device: dict[str, str] = {}
        other_errors: list[str] = []
        for error in result.errors:
            if error.startswith("device:") and error.count(":") >= 2:
                _, device_id, detail = error.split(":", 2)
                by_device[device_id] = detail.strip()
            else:
                other_errors.append(error)
        if other_errors:
            failure = "; ".join(other_errors)
            for device_id in affected:
                by_device.setdefault(device_id, failure)
        self._device_errors = by_device
        devices = tuple(
            DeviceApplyState(
                device_id=device_id,
                target_revision=stored.stored_revision,
                applied=device_id not in by_device,
                error=by_device.get(device_id, ""),
            )
            for device_id in affected
        )
        if not result.success:
            return self._store.mark_applied(
                selector,
                stored.stored_revision,
                error="; ".join(result.errors) or "runtime convergence failed",
                devices=devices,
            )
        if selector.scope == "INSTANCE" and selector.name == "devices":
            self._store.refresh_from_disk()
        return self._store.mark_applied(selector, stored.stored_revision, devices=devices)

    async def retry_failed(self) -> None:
        """Retry stored-but-unapplied instance/template changes without rewriting."""
        async with self._lock:
            for selector in self._store.failed_selectors():
                known = self._store.status(selector)
                await self._apply(selector, known)

    def device_apply_results(self) -> dict[str, str]:
        """Expose affected device failures; empty message means applied."""
        return dict(self._device_errors)

    def list_selectors(self) -> tuple[ConfigSelector, ...]:
        return self._store.list_selectors()

    def status(self, selector: ConfigSelector) -> ConfigMutationResult:
        self._authorize(selector, "read")
        return self._store.status(selector)
