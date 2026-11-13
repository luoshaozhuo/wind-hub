"""ConfigManager Get/Push/Apply and desired-state transaction semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from sink_service.application.config import validate_snapshot
from sink_service.application.config_manager import ConfigManager
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort, DeliveryRegistry
from sink_service.domain.config import (
    CandidateNotFoundError,
    ConfigurationError,
    InvalidConfigurationError,
    RevisionConflictError,
    ValidatedSnapshot,
    WriterRequiredError,
)
from sink_service.infrastructure.config_repository import GenerationStore
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    TEMPLATE_ROOT,
    snapshot_docs,
    snapshot_files,
    wire_files,
    write_tree,
)

pytestmark = [pytest.mark.component, pytest.mark.contract]


class FakePort(DeliveryPort):
    def __init__(self, delivery_id: str, *, fail: bool = False) -> None:
        self.delivery_id = delivery_id
        self.fail = fail
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1
        if self.fail:
            raise RuntimeError(f"{self.delivery_id}: unavailable")

    async def stop(self) -> None:
        self.stopped += 1
        if self.fail:
            raise RuntimeError(f"{self.delivery_id}: stop unavailable")


def _factory(ports: dict[str, FakePort], *, fail_start: set[str] | None = None):
    fail_start = fail_start or set()

    def factory(definition: DeliveryDefinition, snapshot: ValidatedSnapshot) -> FakePort:
        port = FakePort(definition.delivery_id, fail=definition.delivery_id in fail_start)
        ports[definition.delivery_id] = port
        return port

    return factory


class _CombinedRegistry:
    """Test-side equivalent of production WorkerRegistryView fan-out.

    Main owns one ConfigManager whose reconcile must converge Deliveries on
    BOTH worker roles; a single-role registry can never make ``all_ids`` a
    subset of the applied tuple, so effective-revision advancement would hang.
    """

    def __init__(self, snapshot: object, fail_start: set[str] | None = None) -> None:
        self._fail_start = fail_start or set()
        self._ports: dict[str, FakePort] = {}
        self._registries = {
            role: DeliveryRegistry(role, snapshot) for role in ("realtime", "reliable")
        }

    def get(self, delivery_id: str) -> object:
        for registry in self._registries.values():
            try:
                return registry.get(delivery_id)
            except KeyError:
                continue
        raise KeyError(delivery_id)

    async def apply_configuration(self, snapshot: object) -> tuple[str, ...]:
        applied: list[str] = []
        for registry in self._registries.values():
            applied.extend(
                await registry.apply_configuration(
                    snapshot, _factory(self._ports, fail_start=self._fail_start)
                )
            )
        return tuple(sorted(applied))

    @property
    def ports(self) -> dict[str, FakePort]:
        return self._ports


async def _wired_writer(
    tmp_path: Path,
    *,
    fail_start: set[str] | None = None,
) -> tuple[ConfigManager, _CombinedRegistry, GenerationStore, object, dict[str, FakePort]]:
    docs = snapshot_docs(config_root=str(tmp_path))
    write_tree(tmp_path, docs)
    initial = validate_snapshot(snapshot_files(docs))
    store = GenerationStore(tmp_path)
    store.commit(initial, "", old=None)
    registry = _CombinedRegistry(initial, fail_start=fail_start)
    manager = ConfigManager(
        store,
        initial,
        writer=True,
        reconcile=registry.apply_configuration,
    )
    return manager, registry, store, initial, registry.ports


def _candidate(docs: dict) -> list:
    return wire_files(snapshot_files(docs))


async def test_push_validates_and_stages_without_mutating_disk(tmp_path: Path) -> None:
    manager, _registry, _store, initial, _ports = await _wired_writer(tmp_path)
    before = manager.disk.revision
    candidate_id, snapshot = await manager.push(
        _candidate(_stopped_docs(tmp_path, "ems_power")), initial.revision
    )
    assert candidate_id
    assert snapshot.revision != before
    assert manager.disk.revision == before  # Push never mutates disk
    assert manager.candidate(candidate_id).revision == snapshot.revision


def _stopped_docs(tmp_path: Path, delivery_id: str) -> dict:
    docs = snapshot_docs(config_root=str(tmp_path))
    docs[DELIVERIES_INSTANCE]["deliveries"][delivery_id]["desired_state"] = "STOP"
    return docs


async def test_stale_expected_revision_aborts_push(tmp_path: Path) -> None:
    manager, *_ = await _wired_writer(tmp_path)
    with pytest.raises(RevisionConflictError):
        await manager.push(_candidate(snapshot_docs(config_root=str(tmp_path))), "stale")


async def test_invalid_candidate_is_invalid_argument(tmp_path: Path) -> None:
    manager, _r, _s, initial, _p = await _wired_writer(tmp_path)
    docs = snapshot_docs(config_root=str(tmp_path))
    del docs[SERVICE_FILE]
    with pytest.raises(InvalidConfigurationError):
        await manager.push(wire_files(snapshot_files(docs)), initial.revision)
    assert manager.disk.revision == initial.revision


async def test_reader_rejects_mutations(tmp_path: Path) -> None:
    docs = snapshot_docs(config_root=str(tmp_path))
    write_tree(tmp_path, docs)
    initial = validate_snapshot(snapshot_files(docs))
    manager = ConfigManager(None, initial, writer=False)  # type: ignore[arg-type]
    with pytest.raises(WriterRequiredError):
        await manager.push(_candidate(docs), initial.revision)
    with pytest.raises(WriterRequiredError):
        await manager.apply("any", initial.revision)


async def test_candidate_capacity_and_expiry(tmp_path: Path) -> None:
    import datetime as dt

    manager, _r, _s, initial, _p = await _wired_writer(tmp_path)
    manager._max_candidates = 1
    docs = _stopped_docs(tmp_path, "ems_power")
    first, staged = await manager.push(_candidate(docs), initial.revision)
    with pytest.raises(ConfigurationError, match="candidate capacity"):
        await manager.push(_candidate(docs), initial.revision)

    manager._candidates[first] = type(manager._candidates[first])(
        snapshot=staged,
        base_revision=initial.revision,
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
    )
    with pytest.raises(CandidateNotFoundError):
        manager.candidate(first)


async def test_apply_commits_and_advances_runtime_when_converged(tmp_path: Path) -> None:
    manager, registry, _store, initial, ports = await _wired_writer(tmp_path)
    # Initial reconcile was not run at manager construction; emulate startup.
    await registry.apply_configuration(initial)

    docs = _stopped_docs(tmp_path, "ems_power")
    candidate_id, snapshot = await manager.push(_candidate(docs), initial.revision)
    application = await manager.apply(candidate_id, initial.revision)
    assert application.file_commit == snapshot.revision
    assert application.effective_revision == snapshot.revision
    # Combined fan-out re-asserts every converged Delivery across both roles;
    # the stopped Delivery is among the applied set and nothing stays pending.
    assert "ems_power" in application.applied
    assert application.pending == ()
    assert manager.runtime.revision == snapshot.revision
    assert ports["ems_power"].stopped == 1
    assert manager.latest_application is application


async def test_start_desired_change_converges_and_advances(tmp_path: Path) -> None:
    # Regression: a desired-only signature change must report convergence.
    manager, registry, _store, initial, ports = await _wired_writer(tmp_path)
    docs = snapshot_docs(config_root=str(tmp_path))
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    stopped = validate_snapshot(snapshot_files(docs))
    await registry.apply_configuration(stopped)
    manager._disk = stopped
    manager._runtime = stopped

    start_docs = snapshot_docs(config_root=str(tmp_path))
    candidate_id, started_snapshot = await manager.push(_candidate(start_docs), stopped.revision)
    application = await manager.apply(candidate_id, stopped.revision)
    assert application.pending == ()
    assert "ems_power" in application.applied
    assert manager.runtime.revision == started_snapshot.revision
    assert registry.get("ems_power").actual_state == "RUNNING"


async def test_apply_with_failed_convergence_keeps_old_runtime(tmp_path: Path) -> None:
    manager, registry, _store, initial, ports = await _wired_writer(
        tmp_path, fail_start={"ems_power"}
    )
    await registry.apply_configuration(initial)

    # Publish a new immutable template version and repoint the instance: the
    # replacement port is rebuilt and its START fails. Disk commits; runtime
    # stays on the old revision.
    docs = snapshot_docs(config_root=str(tmp_path))
    import copy

    new_template = copy.deepcopy(docs[f"{TEMPLATE_ROOT}/deliveries/delivery_realtime_power_v1.0.0.yaml"])
    new_template["file_version"] = "1.1.0"
    new_template["output"]["period_ms"] = 200
    # Exact-closure rule: the repointed 1.0.0 template becomes unreferenced
    # and must leave the snapshot together with the instance repoint.
    docs.pop(f"{TEMPLATE_ROOT}/deliveries/delivery_realtime_power_v1.0.0.yaml")
    docs[f"{TEMPLATE_ROOT}/deliveries/delivery_realtime_power_v1.1.0.yaml"] = new_template
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["template_version"] = "1.1.0"
    candidate_id, committed = await manager.push(wire_files(snapshot_files(docs)), initial.revision)
    application = await manager.apply(candidate_id, initial.revision)
    assert application.file_commit == committed.revision
    assert manager.disk.revision == committed.revision
    assert manager.runtime.revision == initial.revision
    assert application.pending == ("ems_power",)
    assert application.error
    latest = manager.latest_application
    assert latest is not None and latest.pending == ("ems_power",)


async def test_apply_missing_candidate_is_not_found(tmp_path: Path) -> None:
    manager, *_ = await _wired_writer(tmp_path)
    with pytest.raises(CandidateNotFoundError):
        await manager.apply("missing", manager.disk.revision)


async def test_apply_revision_conflict_aborts_without_commit(tmp_path: Path) -> None:
    manager, _r, _store, initial, _p = await _wired_writer(tmp_path)
    candidate_id, _ = await manager.push(
        _candidate(_stopped_docs(tmp_path, "ems_power")), initial.revision
    )
    with pytest.raises(RevisionConflictError):
        await manager.apply(candidate_id, "someone-else-committed")
    assert manager.disk.revision == initial.revision


async def test_second_apply_of_same_candidate_rejected(tmp_path: Path) -> None:
    manager, _r, _store, initial, _p = await _wired_writer(tmp_path)
    candidate_id, snapshot = await manager.push(
        _candidate(_stopped_docs(tmp_path, "ems_power")), initial.revision
    )
    first = await manager.apply(candidate_id, initial.revision)
    assert first.file_commit == snapshot.revision
    with pytest.raises(CandidateNotFoundError):
        await manager.apply(candidate_id, initial.revision)


async def test_service_change_marks_restart_required(tmp_path: Path) -> None:
    manager, registry, _store, initial, ports = await _wired_writer(tmp_path)
    await registry.apply_configuration(initial)
    docs = snapshot_docs(config_root=str(tmp_path))
    docs[SERVICE_FILE]["grpc"]["max_active_rpcs"] = 32
    candidate_id, snapshot = await manager.push(wire_files(snapshot_files(docs)), initial.revision)
    application = await manager.apply(candidate_id, initial.revision)
    assert application.restart_required is True
    assert manager.runtime.revision == initial.revision


async def test_set_delivery_desired_uses_same_cas_as_apply(tmp_path: Path) -> None:
    manager, registry, _store, initial, ports = await _wired_writer(tmp_path)
    await registry.apply_configuration(initial)
    snapshot, commit = await manager.set_delivery_desired(
        "ems_power", running=False, expected_revision=initial.revision
    )
    assert commit == snapshot.revision
    assert snapshot.deliveries["ems_power"]["desired_state"] == "STOP"
    with pytest.raises(RevisionConflictError):
        await manager.set_delivery_desired(
            "ems_power", running=True, expected_revision=initial.revision
        )
    with pytest.raises(KeyError):
        await manager.set_delivery_desired(
            "ghost", running=True, expected_revision=snapshot.revision
        )


def test_construction_pins_disk_and_runtime_to_initial_revision(tmp_path: Path) -> None:
    # v1_20 has no separate reader process: a single Main owns disk and runtime,
    # and both revisions start at the committed generation (mark_active removed).
    docs = snapshot_docs(config_root=str(tmp_path))
    write_tree(tmp_path, docs)
    initial = validate_snapshot(snapshot_files(docs))
    manager = ConfigManager(None, initial, writer=False)  # type: ignore[arg-type]
    assert manager.runtime.revision == initial.revision
    assert manager.disk.revision == initial.revision
