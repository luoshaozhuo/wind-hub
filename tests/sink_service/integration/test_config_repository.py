"""Crash-safety and immutability of GenerationStore on a real filesystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from sink_service.application.config import validate_snapshot
from sink_service.domain.config import (
    ConfigurationError,
    RevisionConflictError,
)
from sink_service.infrastructure.config_repository import GenerationStore
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
    snapshot_docs,
    snapshot_files,
    template_doc,
    write_tree,
)

pytestmark = [pytest.mark.integration, pytest.mark.file]


def _initial(root: Path) -> tuple[GenerationStore, object]:
    docs = snapshot_docs(config_root=str(root))
    write_tree(root, docs)
    files = snapshot_files(docs)
    snapshot = validate_snapshot(files)
    store = GenerationStore(root)
    store.commit(snapshot, "", old=None)
    return store, snapshot


def test_first_commit_publishes_atomic_current_pointer(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    pointer = (tmp_path / "CURRENT").read_text(encoding="ascii").strip()
    assert pointer == snapshot.revision
    generation = tmp_path / ".generations" / snapshot.revision
    assert generation.is_dir()
    loaded = store.load(SERVICE_FILE)
    assert loaded.revision == snapshot.revision


def test_load_observes_complete_generation(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    loaded = store.load(SERVICE_FILE)
    # Every managed file of the generation is reachable through one pointer.
    assert {f.path for f in loaded.files} == {f.path for f in snapshot.files}


def test_commit_rejects_stale_expected_revision(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    with pytest.raises(RevisionConflictError):
        store.commit(snapshot, "", old=snapshot)
    with pytest.raises(RevisionConflictError):
        store.commit(snapshot, "deadbeef", old=snapshot)


def test_published_template_version_is_immutable(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    docs = snapshot_docs(config_root=str(tmp_path))
    target = f"{TEMPLATE_ROOT}/sinks/sink_redis_realtime_v1.0.0.yaml"
    docs[target]["output"]["namespace"] = "tampered"
    files = snapshot_files(docs)
    changed = validate_snapshot(files)
    with pytest.raises(ConfigurationError, match="immutable template"):
        store.commit(changed, snapshot.revision, old=snapshot)


def test_unreferenced_template_violates_closure(tmp_path: Path) -> None:
    # v1_20 snapshots are exact closures: an unmanaged template file is rejected.
    docs = snapshot_docs(config_root=str(tmp_path))
    extra = template_doc("redis_realtime")
    extra["file_version"] = "0.9.0"
    docs[f"{TEMPLATE_ROOT}/sinks/sink_redis_realtime_v0.9.0.yaml"] = extra
    with pytest.raises(ConfigurationError, match="closure mismatch"):
        validate_snapshot(snapshot_files(docs))


def test_template_version_upgrade_keeps_old_generation_bytes(tmp_path: Path) -> None:
    # First generation references redis_realtime 0.9.0.
    old_docs = snapshot_docs(config_root=str(tmp_path))
    legacy_path = f"{TEMPLATE_ROOT}/sinks/sink_redis_realtime_v0.9.0.yaml"
    legacy = template_doc("redis_realtime")
    legacy["file_version"] = "0.9.0"
    old_docs.pop(f"{TEMPLATE_ROOT}/sinks/sink_redis_realtime_v1.0.0.yaml")
    old_docs[legacy_path] = legacy
    old_docs[SINKS_INSTANCE]["sinks"]["ems_redis"]["template_version"] = "0.9.0"
    write_tree(tmp_path, old_docs)
    first = validate_snapshot(snapshot_files(old_docs))
    store = GenerationStore(tmp_path)
    store.commit(first, "", old=None)
    legacy_bytes = (tmp_path / ".generations" / first.revision / legacy_path).read_bytes()

    # Follow-up generation moves the Sink to 1.0.0 and drops the 0.9.0 reference.
    new_docs = snapshot_docs(config_root=str(tmp_path))
    new_docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    second = validate_snapshot(snapshot_files(new_docs))
    committed = store.commit(second, first.revision, old=first)
    assert committed == second.revision

    # The old immutable generation remains byte-for-byte on disk.
    assert (tmp_path / ".generations" / first.revision / legacy_path).read_bytes() == legacy_bytes
    assert store.load(SERVICE_FILE).revision == committed


def test_new_template_version_and_revision_commit_is_loadable(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    docs = snapshot_docs(config_root=str(tmp_path))
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    new_snapshot = validate_snapshot(snapshot_files(docs))
    committed = store.commit(new_snapshot, snapshot.revision, old=snapshot)
    assert committed == new_snapshot.revision
    assert (tmp_path / "CURRENT").read_text(encoding="ascii").strip() == committed
    reloaded = store.load(SERVICE_FILE)
    assert reloaded.revision == committed


def test_generation_identifier_must_be_sha256(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path)
    (tmp_path / "CURRENT").write_text("not-a-sha256\n", encoding="ascii")
    with pytest.raises(ConfigurationError):
        store.load(SERVICE_FILE)


def test_rejected_commit_leaves_current_untouched(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    before = (tmp_path / "CURRENT").read_text(encoding="ascii")
    docs = snapshot_docs(config_root=str(tmp_path))
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    new_snapshot = validate_snapshot(snapshot_files(docs))
    with pytest.raises(RevisionConflictError):
        store.commit(new_snapshot, "stale", old=snapshot)
    assert (tmp_path / "CURRENT").read_text(encoding="ascii") == before
    # The old generation still loads completely.
    assert store.load(SERVICE_FILE).revision == snapshot.revision


def test_load_rejects_service_path_escape(tmp_path: Path) -> None:
    store, _ = _initial(tmp_path)
    with pytest.raises(ConfigurationError):
        store.load("configs/services/../sink_service.yaml")
