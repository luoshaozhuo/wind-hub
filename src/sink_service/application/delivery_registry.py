"""Delivery lifecycle state with revision-safe operator intent."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from sink_service.application.config import template_document, template_path
from sink_service.application.delivery_definition import DeliveryDefinition, delivery_definitions
from sink_service.application.latency_window import HistogramSnapshot, LatencyWindow
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot, config_sources


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

    @property
    def last_error(self) -> str: ...


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
    removing: bool = False


class DeliveryRegistry:
    """One process owns only the Deliveries matching its channel."""

    def __init__(self, process_role: str, snapshot: ValidatedSnapshot) -> None:
        if process_role not in ("realtime", "reliable"):
            raise ConfigurationError("invalid process role")
        self._role = process_role
        self._revision = snapshot.revision
        self._entries: dict[str, _Entry] = {}
        self._delivery_epochs: dict[str, datetime] = {}
        self._retired_counters: dict[str, float] = {}
        self._retired_by_delivery: dict[str, dict[str, float]] = {}
        self._retired_latency_by_delivery: dict[str, LatencyWindow] = {}
        self._metrics_window_ms = snapshot.service["metrics"]["metrics_window_ms"]
        self._retired_latencies = LatencyWindow(window_ms=self._metrics_window_ms)
        self._lock = asyncio.Lock()
        self._pending_snapshot = snapshot
        self._pending_factory: (
            Callable[[DeliveryDefinition, ValidatedSnapshot], DeliveryPort] | None
        ) = None

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
        _, template_root = config_sources(snapshot.service)
        for identifier, item in definitions.items():
            delivery = snapshot.deliveries[identifier]
            sink = snapshot.sinks[item.sink_id]
            template_paths = {
                template_path(
                    template_root,
                    "deliveries",
                    delivery["template_id"],
                    delivery["template_version"],
                ),
                template_path(
                    template_root,
                    "sinks",
                    sink["template_id"],
                    sink["template_version"],
                ),
            }
            sink_kind = template_document(
                snapshot, "sinks", sink["template_id"], sink["template_version"]
            )["kind"]
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
                "shared_modbus_mapping": (
                    {
                        name: value["source"]
                        for name, value in sorted(snapshot.deliveries.items())
                        if value["sink_id"] == item.sink_id
                    }
                    if sink_kind == "modbus_tcp"
                    else {}
                ),
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
            self._pending_snapshot = snapshot
            self._pending_factory = factory
            for identifier in sorted(set(self._entries) - set(definitions)):
                entry = self._entries[identifier]
                entry.view = self._replace(
                    entry.view, actual_state=ActualState.STOPPING, last_error=""
                )
                try:
                    await entry.port.stop()
                except Exception as exc:
                    entry.removing = True
                    entry.view = self._replace(
                        entry.view, actual_state=ActualState.FAILED, last_error=str(exc)
                    )
                    continue
                self._retire(entry)
                del self._entries[identifier]
                changed.append(identifier)
            # Drain all changed users of a shared Sink before rebinding any.
            for identifier in sorted(definitions):
                current_entry = self._entries.get(identifier)
                if current_entry is None:
                    continue
                entry = current_entry
                if entry.signature == signatures[identifier]:
                    continue
                try:
                    await entry.port.stop()
                except Exception as exc:
                    entry.removing = True
                    entry.view = self._replace(
                        entry.view, actual_state=ActualState.FAILED, last_error=str(exc)
                    )
                    continue
                self._retire(entry)
                del self._entries[identifier]
                changed.append(identifier)
            for identifier, definition in definitions.items():
                existing = self._entries.get(identifier)
                signature = signatures[identifier]
                if (
                    existing is not None
                    and existing.signature == signature
                    and not existing.removing
                ):
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
                        # A signature-preserving change is still a real
                        # convergence (e.g. operator desired_state change).
                        changed.append(identifier)
                    except Exception:
                        # The FAILED state and error remain queryable.
                        changed.append(identifier)
                    continue
                # A failed old port retains ownership until a later retry.
                if existing is not None:
                    continue
                self._delivery_epochs.setdefault(identifier, datetime.now(UTC))
                replacement = factory(definition, snapshot)
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
            if (
                identifier not in self._entries
                or (
                    not self._entries[identifier].removing
                    and self._entries[identifier].view.revision == snapshot.revision
                    and self._entries[identifier].view.actual_state
                    == (
                        ActualState.RUNNING
                        if self._entries[identifier].view.desired_state == "START"
                        else ActualState.STOPPED
                    )
                    and not self._entries[identifier].port.last_error
                )
            )
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
        self._delivery_epochs.setdefault(delivery_id, datetime.now(UTC))
        self._entries[delivery_id] = _Entry(port=port, view=view)

    @staticmethod
    def _visible(entry: _Entry) -> DeliveryView:
        """Surface asynchronous output degradation without releasing ownership."""
        view = entry.view
        error = entry.port.last_error
        if error and view.actual_state == ActualState.RUNNING:
            return DeliveryView(
                delivery_id=view.delivery_id,
                channel=view.channel,
                sink_id=view.sink_id,
                process_role=view.process_role,
                desired_state=view.desired_state,
                actual_state=ActualState.FAILED,
                revision=view.revision,
                last_error=error,
            )
        return view

    def _retire(self, entry: _Entry) -> None:
        """Preserve monotonic counters when a Delivery binding is replaced."""
        identifier = entry.view.delivery_id
        previous = self._retired_by_delivery.setdefault(identifier, {})
        for name, value in getattr(entry.port, "metrics", {}).items():
            if name.endswith("_total"):
                previous[name] = previous.get(name, 0.0) + float(value)
            if name.endswith("_total"):
                self._retired_counters[name] = self._retired_counters.get(name, 0.0) + float(value)
        history = self._retired_latency_by_delivery.setdefault(
            identifier, LatencyWindow(window_ms=self._metrics_window_ms)
        )
        for snapshot in self._port_latency_snapshots(entry.port):
            self._retired_latencies.absorb_snapshot(snapshot)
            history.absorb_snapshot(snapshot)

    def counters(self) -> dict[str, float]:
        """Aggregate counters and only gauges verified for all active owners."""
        result = dict(self._retired_counters)
        reliable_count = 0
        backlog_count = 0
        for entry in self._entries.values():
            values = getattr(entry.port, "metrics", {})
            if entry.view.channel == "reliable" and entry.view.desired_state == "START":
                reliable_count += 1
                backlog_count += int("backlog_messages" in values)
            for name, value in values.items():
                result[name] = result.get(name, 0.0) + float(value)
        if reliable_count != backlog_count:
            result.pop("backlog_messages", None)
        for name in (
            "received_total",
            "delivered_total",
            "failed_total",
            "dropped_total",
            "dead_letter_total",
            "write_success_total",
        ):
            result.setdefault(name, 0.0)
        result.setdefault("io_inflight", 0.0)
        return result

    def delivery_epochs(self) -> dict[str, datetime]:
        """Return stable metric epochs for active Delivery identities."""
        return {identifier: self._delivery_epochs[identifier] for identifier in self._entries}

    @staticmethod
    def _port_latency_snapshots(port: DeliveryPort) -> tuple[HistogramSnapshot, ...]:
        combined = getattr(port, "latency_histograms", None)
        if combined is not None:
            return cast(tuple[HistogramSnapshot, ...], combined)
        window = getattr(port, "latency_window", None)
        return (cast(LatencyWindow, window).snapshot(),) if window is not None else ()

    def delivery_observations(
        self,
    ) -> tuple[dict[str, dict[str, float]], dict[str, tuple[HistogramSnapshot, ...]]]:
        """Read bounded latency histograms for each active Delivery identity."""
        counters: dict[str, dict[str, float]] = {}
        histories: dict[str, tuple[HistogramSnapshot, ...]] = {}
        for identifier, entry in self._entries.items():
            values = dict(self._retired_by_delivery.get(identifier, {}))
            for name, value in getattr(entry.port, "metrics", {}).items():
                if name.endswith("_total"):
                    values[name] = values.get(name, 0.0) + float(value)
                else:
                    values[name] = float(value)
            counters[identifier] = values
            retired = self._retired_latency_by_delivery.get(identifier)
            histories[identifier] = (
                (retired.snapshot(),) if retired is not None else ()
            ) + self._port_latency_snapshots(entry.port)
        live = set(self._entries)
        for identifier in tuple(self._retired_by_delivery):
            if identifier not in live:
                self._retired_by_delivery.pop(identifier, None)
                self._retired_latency_by_delivery.pop(identifier, None)
                self._delivery_epochs.pop(identifier, None)
        return counters, histories

    def latency_histograms(self) -> tuple[HistogramSnapshot, ...]:
        snapshots = [self._retired_latencies.snapshot()]
        for entry in self._entries.values():
            snapshots.extend(self._port_latency_snapshots(entry.port))
        return tuple(snapshots)

    def list(self, channel: str = "") -> tuple[DeliveryView, ...]:
        if channel and channel != self._role:
            return ()
        return tuple(self._visible(self._entries[key]) for key in sorted(self._entries))

    def inspect(self, delivery_id: str) -> dict[str, str]:
        """Read bounded process-owned evidence without interacting with a target."""
        entry = self._entries.get(delivery_id)
        if entry is None:
            raise KeyError(f"unknown Delivery: {delivery_id}")
        method = getattr(entry.port, "inspect", None)
        return dict(method()) if callable(method) else {}

    def get(self, delivery_id: str) -> DeliveryView:
        try:
            return self._visible(self._entries[delivery_id])
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
        """Close all ports even if one refuses to terminate."""
        async with self._lock:
            failures: list[BaseException] = []
            for entry in self._entries.values():
                try:
                    await entry.port.stop()
                except BaseException as exc:
                    failures.append(exc)
                    entry.view = self._replace(
                        entry.view, actual_state=ActualState.FAILED, last_error=str(exc)
                    )
                else:
                    entry.view = self._replace(
                        entry.view, actual_state=ActualState.STOPPED, last_error=""
                    )
            if failures:
                raise BaseExceptionGroup("Delivery shutdown failed", failures)

    async def reconcile_all(self) -> None:
        """Retry STOP/retirement first, then reconcile only desired START."""
        retired = False
        async with self._lock:
            for identifier, entry in tuple(self._entries.items()):
                if entry.removing:
                    # A retiring port must settle before its binding can change.
                    try:
                        await entry.port.stop()
                    except Exception as exc:
                        entry.view = self._replace(
                            entry.view,
                            actual_state=ActualState.FAILED,
                            last_error=str(exc),
                        )
                        continue
                    self._retire(entry)
                    del self._entries[identifier]
                    retired = True
                    continue
                if entry.view.desired_state == "STOP":
                    if entry.view.actual_state != ActualState.STOPPED:
                        try:
                            await self._reconcile(entry)
                        except Exception:
                            continue
                    continue
                if entry.view.actual_state == ActualState.RUNNING and entry.port.last_error:
                    try:
                        await entry.port.stop()
                        entry.view = self._replace(
                            entry.view,
                            actual_state=ActualState.STOPPED,
                            last_error="",
                        )
                    except Exception as exc:
                        entry.view = self._replace(
                            entry.view,
                            actual_state=ActualState.FAILED,
                            last_error=str(exc),
                        )
                        continue
                if entry.view.actual_state not in (ActualState.STOPPED, ActualState.FAILED):
                    continue
                try:
                    await self._reconcile(entry)
                except Exception:
                    continue
        if retired and self._pending_factory is not None:
            # Rebuild only definitions present in the latest committed view.
            await self.apply_configuration(self._pending_snapshot, self._pending_factory)

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
        except BaseException as exc:
            if not running:
                # A failed STOP may still own native I/O. Never START this
                # binding again before it has been safely retired.
                entry.removing = True
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
