"""Delivery lifecycle state with revision-safe operator intent."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sink_service.application.delivery_definition import DeliveryDefinition, delivery_definitions
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot


class ActualState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class DeliveryPort(Protocol):
    """Transport-specific lifecycle; no protocol types escape this boundary."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveryView:
    delivery_id: str
    channel: str
    sink_id: str
    process_role: str
    desired_state: str
    actual_state: str
    revision: str
    last_error: str


@dataclass(slots=True)
class _Entry:
    port: DeliveryPort
    view: DeliveryView
    signature: str = ""


class DeliveryRegistry:
    """One process owns only the Deliveries matching its channel."""

    def __init__(self, process_role: str, snapshot: ValidatedSnapshot) -> None:
        if process_role not in ("realtime", "reliable"):
            raise ConfigurationError("invalid process role")
        self._role = process_role
        self._revision = snapshot.revision
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def apply_configuration(
        self,
        snapshot: ValidatedSnapshot,
        factory: Callable[[DeliveryDefinition, ValidatedSnapshot], DeliveryPort],
    ) -> tuple[str, ...]:
        """Incrementally reconcile changed, added and removed Deliveries.

        Only changed bindings close their old ports. An unchanged STOP remains
        stopped and is never revived by a Watchdog.
        """
        definitions = {
            item.delivery_id: item
            for item in delivery_definitions(snapshot)
            if item.channel == self._role
        }
        signatures: dict[str, str] = {}
        for identifier, item in definitions.items():
            template_paths = {
                f"templates/deliveries/{snapshot.deliveries[identifier]['template_id']}/"
                f"{snapshot.deliveries[identifier]['template_version']}.yaml",
                f"templates/sinks/{snapshot.sinks[item.sink_id]['template_id']}/"
                f"{snapshot.sinks[item.sink_id]['template_version']}.yaml",
            }
            raw = {
                "templates": {
                    file.path: hashlib.sha256(file.content).hexdigest()
                    for file in snapshot.files
                    if file.path in template_paths
                },
                "delivery": {
                    key: value
                    for key, value in snapshot.deliveries[identifier].items()
                    if key != "desired_state"
                },
                "sink": snapshot.sinks[item.sink_id],
                "delivery_template": (
                    snapshot.deliveries[identifier]["template_id"],
                    snapshot.deliveries[identifier]["template_version"],
                ),
            }
            signatures[identifier] = hashlib.sha256(
                json.dumps(raw, sort_keys=True).encode("utf-8")
            ).hexdigest()
        changed: list[str] = []
        async with self._lock:
            for identifier in sorted(set(self._entries) - set(definitions)):
                entry = self._entries[identifier]
                await entry.port.stop()
                del self._entries[identifier]
                changed.append(identifier)
            for identifier, definition in definitions.items():
                existing = self._entries.get(identifier)
                signature = signatures[identifier]
                if existing is not None and existing.signature == signature:
                    old = existing.view
                    existing.view = DeliveryView(
                        delivery_id=old.delivery_id,
                        channel=old.channel,
                        sink_id=old.sink_id,
                        process_role=old.process_role,
                        desired_state=definition.desired_state,
                        actual_state=old.actual_state,
                        revision=snapshot.revision,
                        last_error=old.last_error,
                    )
                    try:
                        await self._reconcile(existing)
                    except Exception:
                        # The FAILED state and error remain queryable.
                        changed.append(identifier)
                    continue
                # Construct the replacement before closing the active binding.
                replacement = factory(definition, snapshot)
                if existing is not None:
                    await existing.port.stop()
                self._entries[identifier] = _Entry(
                    port=replacement,
                    view=DeliveryView(
                        delivery_id=identifier,
                        channel=definition.channel,
                        sink_id=definition.sink_id,
                        process_role=self._role,
                        desired_state=definition.desired_state,
                        actual_state=ActualState.STOPPED,
                        revision=snapshot.revision,
                        last_error="",
                    ),
                    signature=signature,
                )
                changed.append(identifier)
                try:
                    await self._reconcile(self._entries[identifier])
                except Exception:
                    # Keep the failed Delivery registered for management queries.
                    continue
            self._revision = snapshot.revision
        return tuple(
            identifier
            for identifier in changed
            if identifier not in self._entries
            or self._entries[identifier].view.actual_state != ActualState.FAILED
        )

    @property
    def revision(self) -> str:
        return self._revision

    def register(
        self,
        delivery_id: str,
        *,
        channel: str,
        sink_id: str,
        desired_state: str,
        port: DeliveryPort,
    ) -> None:
        if channel != self._role:
            raise ConfigurationError("cannot register another process's Delivery")
        if desired_state not in ("START", "STOP"):
            raise ConfigurationError("invalid desired state")
        if delivery_id in self._entries:
            raise ConfigurationError("duplicate Delivery")
        view = DeliveryView(
            delivery_id=delivery_id,
            channel=channel,
            sink_id=sink_id,
            process_role=self._role,
            desired_state=desired_state,
            actual_state=ActualState.STOPPED,
            revision=self._revision,
            last_error="",
        )
        self._entries[delivery_id] = _Entry(port=port, view=view)

    def list(self, channel: str = "") -> tuple[DeliveryView, ...]:
        if channel and channel != self._role:
            return ()
        return tuple(self._entries[key].view for key in sorted(self._entries))

    def get(self, delivery_id: str) -> DeliveryView:
        try:
            return self._entries[delivery_id].view
        except KeyError as exc:
            raise KeyError(f"unknown Delivery: {delivery_id}") from exc

    async def accept_revision(self, revision: str) -> None:
        """Advance reconciler revision after the Writer commits its intent."""
        async with self._lock:
            self._revision = revision
            for entry in self._entries.values():
                old = entry.view
                entry.view = DeliveryView(
                    delivery_id=old.delivery_id,
                    channel=old.channel,
                    sink_id=old.sink_id,
                    process_role=old.process_role,
                    desired_state=old.desired_state,
                    actual_state=old.actual_state,
                    revision=revision,
                    last_error=old.last_error,
                )

    async def set_desired(
        self, delivery_id: str, *, running: bool, expected_revision: str
    ) -> DeliveryView:
        """Only called after the Writer durably commits the new desired_state.

        In-process state is not itself the source of truth; the Writer must
        transactionally persist intent and then call this reconciler.
        """
        async with self._lock:
            if expected_revision != self._revision:
                raise ConfigurationError("stale configuration revision")
            entry = self._entries.get(delivery_id)
            if entry is None:
                raise KeyError(f"unknown Delivery: {delivery_id}")
            desired = "START" if running else "STOP"
            old = entry.view
            entry.view = DeliveryView(
                delivery_id=old.delivery_id,
                channel=old.channel,
                sink_id=old.sink_id,
                process_role=old.process_role,
                desired_state=desired,
                actual_state=old.actual_state,
                revision=old.revision,
                last_error=old.last_error,
            )
            await self._reconcile(entry)
            return entry.view

    async def shutdown(self) -> None:
        """Close every process-owned port without changing persisted STOP intent."""
        async with self._lock:
            for entry in self._entries.values():
                try:
                    await entry.port.stop()
                finally:
                    old = entry.view
                    entry.view = self._replace(
                        old, actual_state=ActualState.STOPPED, last_error=""
                    )

    async def reconcile_all(self) -> None:
        """Restore START intent, never resurrect an operator STOP."""
        async with self._lock:
            for entry in self._entries.values():
                await self._reconcile(entry)

    async def _reconcile(self, entry: _Entry) -> None:
        old = entry.view
        running = old.desired_state == "START"
        if running and old.actual_state == ActualState.RUNNING:
            return
        if not running and old.actual_state == ActualState.STOPPED:
            return
        target = ActualState.STARTING if running else ActualState.STOPPING
        entry.view = self._replace(entry.view, actual_state=target, last_error="")
        try:
            if running:
                await entry.port.start()
            else:
                await entry.port.stop()
        except Exception as exc:
            entry.view = self._replace(
                entry.view, actual_state=ActualState.FAILED, last_error=str(exc)
            )
            raise
        entry.view = self._replace(
            entry.view,
            actual_state=ActualState.RUNNING if running else ActualState.STOPPED,
            last_error="",
        )

    @staticmethod
    def _replace(
        value: DeliveryView, *, actual_state: ActualState, last_error: str
    ) -> DeliveryView:
        return DeliveryView(
            delivery_id=value.delivery_id,
            channel=value.channel,
            sink_id=value.sink_id,
            process_role=value.process_role,
            desired_state=value.desired_state,
            actual_state=actual_state,
            revision=value.revision,
            last_error=last_error,
        )
