"""DeliveryRegistry lifecycle, watchdog and resource ownership rules."""

from __future__ import annotations

import pytest

from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort, DeliveryRegistry
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
    snapshot_docs,
    snapshot_files,
)

pytestmark = [pytest.mark.component, pytest.mark.reliability]


class FakePort(DeliveryPort):
    def __init__(
        self,
        delivery_id: str,
        *,
        start_fails: bool = False,
        stop_fails: bool = False,
    ) -> None:
        self.delivery_id = delivery_id
        self.start_fails = start_fails
        self.stop_fails = stop_fails
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1
        if self.start_fails:
            raise RuntimeError(f"{self.delivery_id}: start refused")

    async def stop(self) -> None:
        self.stopped += 1
        if self.stop_fails:
            raise RuntimeError(f"{self.delivery_id}: stop refused")


def _snapshot(desired: dict[str, str] | None = None) -> ValidatedSnapshot:
    docs = snapshot_docs()
    if desired is not None:
        for delivery_id, state in desired.items():
            docs[DELIVERIES_INSTANCE]["deliveries"][delivery_id]["desired_state"] = state
    return validate_snapshot(snapshot_files(docs))


def _factory(fail_start: set[str] | None = None, fail_stop: set[str] | None = None):
    fail_start = fail_start or set()
    fail_stop = fail_stop or set()
    created: dict[str, FakePort] = {}

    def factory(definition: DeliveryDefinition, snapshot: ValidatedSnapshot) -> FakePort:
        port = FakePort(
            definition.delivery_id,
            start_fails=definition.delivery_id in fail_start,
            stop_fails=definition.delivery_id in fail_stop,
        )
        created[definition.delivery_id] = port
        return port

    return factory, created


async def test_registry_owns_only_matching_channel() -> None:
    snapshot = _snapshot()
    registry = DeliveryRegistry("realtime", snapshot)
    factory, created = _factory()
    applied = await registry.apply_configuration(snapshot, factory)
    assert set(applied) == {"ems_power"}
    assert [view.delivery_id for view in registry.list()] == ["ems_power"]
    assert created["ems_power"].started == 1
    view = registry.get("ems_power")
    assert view.actual_state == "RUNNING"
    assert view.channel == "realtime"


async def test_other_role_registry_does_not_start_delivery() -> None:
    snapshot = _snapshot()
    registry = DeliveryRegistry("reliable", snapshot)
    factory, created = _factory()
    applied = await registry.apply_configuration(snapshot, factory)
    assert set(applied) == {"power_history"}
    assert "power_history" in created


async def test_start_failure_is_failed_and_remains_queryable() -> None:
    snapshot = _snapshot()
    registry = DeliveryRegistry("realtime", snapshot)
    factory, created = _factory(fail_start={"ems_power"})
    applied = await registry.apply_configuration(snapshot, factory)
    assert "ems_power" not in applied
    assert registry.get("ems_power").actual_state == "FAILED"
    assert "start refused" in registry.get("ems_power").last_error
    # Watchdog retries a failed START intent.
    await registry.reconcile_all()
    assert created["ems_power"].started == 2
    assert registry.get("ems_power").actual_state == "FAILED"


async def test_watchdog_never_revives_manual_stop() -> None:
    docs = snapshot_docs()
    first = validate_snapshot(snapshot_files(docs))
    registry = DeliveryRegistry("realtime", first)
    factory, created = _factory()
    await registry.apply_configuration(first, factory)
    assert created["ems_power"].started == 1

    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    stopped = validate_snapshot(snapshot_files(docs))
    applied = await registry.apply_configuration(stopped, factory)
    assert "ems_power" in applied
    assert registry.get("ems_power").actual_state == "STOPPED"
    assert created["ems_power"].stopped == 1
    await registry.reconcile_all()
    assert created["ems_power"].started == 1  # not resurrected


async def test_reapply_unchanged_snapshot_does_not_rebuild_port() -> None:
    snapshot = _snapshot()
    registry = DeliveryRegistry("realtime", snapshot)
    factory, created = _factory()
    await registry.apply_configuration(snapshot, factory)
    applied = await registry.apply_configuration(snapshot, factory)
    # Desired unchanged; the delivery is already RUNNING, reported converged,
    # and the physical port is never rebuilt.
    assert "ems_power" in applied
    assert created["ems_power"].started == 1


async def test_changed_binding_closes_old_port_before_replacement_runs() -> None:
    docs = snapshot_docs()
    first = validate_snapshot(snapshot_files(docs))
    registry = DeliveryRegistry("realtime", first)
    factory, created = _factory()
    await registry.apply_configuration(first, factory)
    old_port = created["ems_power"]

    docs[f"{TEMPLATE_ROOT}/deliveries/delivery_realtime_power_v1.0.0.yaml"]["output"]["period_ms"] = 200
    changed = validate_snapshot(snapshot_files(docs))
    await registry.apply_configuration(changed, factory)
    assert old_port.stopped == 1
    assert created["ems_power"].started == 1
    assert created["ems_power"] is not old_port


async def test_removed_delivery_stops_and_releases_ownership() -> None:
    docs = snapshot_docs()
    first = validate_snapshot(snapshot_files(docs))
    registry = DeliveryRegistry("realtime", first)
    factory, created = _factory()
    await registry.apply_configuration(first, factory)

    del docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]
    # Exact-closure rule: the removed Delivery takes its unreferenced Sink and
    # both private templates out of the snapshot.
    del docs[SINKS_INSTANCE]["sinks"]["ems_redis"]
    del docs[f"{TEMPLATE_ROOT}/deliveries/delivery_realtime_power_v1.0.0.yaml"]
    del docs[f"{TEMPLATE_ROOT}/sinks/sink_redis_realtime_v1.0.0.yaml"]
    changed = validate_snapshot(snapshot_files(docs))
    applied = await registry.apply_configuration(changed, factory)
    assert "ems_power" in applied
    with pytest.raises(KeyError):
        registry.get("ems_power")
    assert created["ems_power"].stopped == 1


async def test_failed_removal_retains_ownership_and_watchdog_does_not_restart() -> None:
    docs = snapshot_docs()
    first = validate_snapshot(snapshot_files(docs))
    registry = DeliveryRegistry("realtime", first)
    factory, created = _factory(fail_stop={"ems_power"})
    await registry.apply_configuration(first, factory)

    del docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]
    del docs[SINKS_INSTANCE]["sinks"]["ems_redis"]
    del docs[f"{TEMPLATE_ROOT}/deliveries/delivery_realtime_power_v1.0.0.yaml"]
    del docs[f"{TEMPLATE_ROOT}/sinks/sink_redis_realtime_v1.0.0.yaml"]
    changed = validate_snapshot(snapshot_files(docs))
    applied = await registry.apply_configuration(changed, factory)
    assert "ems_power" not in applied  # still FAILED, ownership retained
    assert registry.get("ems_power").actual_state == "FAILED"
    stopped_once = created["ems_power"].stopped

    # Watchdog retries the failed stop but must never resurrect the removal:
    # no START is issued while STOP keeps failing.
    await registry.reconcile_all()
    assert created["ems_power"].started == 1
    assert created["ems_power"].stopped >= stopped_once

    # Next reconciliation retries stop and releases ownership once it succeeds.
    created["ems_power"].stop_fails = False
    await registry.apply_configuration(changed, factory)
    with pytest.raises(KeyError):
        registry.get("ems_power")
    assert created["ems_power"].stopped > stopped_once
    assert created["ems_power"].started == 1


async def test_shutdown_closes_all_ports_and_aggregates_failures() -> None:
    snapshot = _snapshot(desired={"ems_power": "START", "power_history": "STOP"})
    registry = DeliveryRegistry("realtime", snapshot)
    factory, created = _factory(fail_stop={"ems_power"})
    await registry.apply_configuration(snapshot, factory)
    with pytest.raises(BaseExceptionGroup) as excinfo:
        await registry.shutdown()
    assert "Delivery shutdown failed" in str(excinfo.value)
    assert registry.get("ems_power").actual_state == "FAILED"


async def test_registry_rejects_foreign_role_construction() -> None:
    with pytest.raises(ConfigurationError):
        DeliveryRegistry("historian", _snapshot())


async def test_failed_start_not_revived_when_desired_stop() -> None:
    snapshot = _snapshot()
    registry = DeliveryRegistry("realtime", snapshot)
    factory, created = _factory(fail_start={"ems_power"})
    await registry.apply_configuration(snapshot, factory)
    assert registry.get("ems_power").actual_state == "FAILED"

    docs = snapshot_docs()
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    stopped = validate_snapshot(snapshot_files(docs))
    applied = await registry.apply_configuration(stopped, factory)
    assert "ems_power" in applied
    assert registry.get("ems_power").actual_state == "STOPPED"
    await registry.reconcile_all()
    assert created["ems_power"].started == 1
