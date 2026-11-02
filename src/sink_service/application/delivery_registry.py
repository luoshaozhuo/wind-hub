"""Delivery lifecycle state with revision-safe operator intent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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


class DeliveryRegistry:
    """One process owns only the Deliveries matching its channel."""

    def __init__(self, process_role: str, snapshot: ValidatedSnapshot) -> None:
        if process_role not in ("realtime", "reliable"):
            raise ConfigurationError("invalid process role")
        self._role = process_role
        self._revision = snapshot.revision
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

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
