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
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files, write_tree

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
    loaded = store.load("services/sink-realtime.yaml")
    assert loaded.revision == snapshot.revision


def test_reader_only_obsvers_complete_generation(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    loaded = store.load("services/sink-reliable.yaml")
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
    target = "templates/sinks/redis_realtime/1.0.0.yaml"
    docs[target]["output"]["namespace"] = "tampered"
    files = snapshot_files(docs)
    changed = validate_snapshot(files)
    with pytest.raises(ConfigurationError, match="immutable template"):
        store.commit(changed, snapshot.revision, old=snapshot)


def test_published_template_version_cannot_be_deleted(tmp_path: Path) -> None:
    # Publish a generation that also carries an unused template version.
    docs = snapshot_docs(config_root=str(tmp_path))
    extra = dict(docs["templates/sinks/redis_realtime/1.0.0.yaml"])
    extra["output"]["namespace"] = "legacy"
    docs["templates/sinks/redis_realtime/0.9.0.yaml"] = extra
    write_tree(tmp_path, docs)
    snapshot = validate_snapshot(snapshot_files(docs))
    store = GenerationStore(tmp_path)
    store.commit(snapshot, "", old=None)

    new_docs = snapshot_docs(config_root=str(tmp_path))
    new_docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    # The follow-up generation drops the previously published 0.9.0 template.
    with pytest.raises(ConfigurationError, match="published template version deleted"):
        store.commit(
            validate_snapshot(snapshot_files(new_docs)),
            snapshot.revision,
            old=snapshot,
        )


def test_new_template_version_and_revision_commit_is_loadable(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    docs = snapshot_docs(config_root=str(tmp_path))
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    new_snapshot = validate_snapshot(snapshot_files(docs))
    committed = store.commit(new_snapshot, snapshot.revision, old=snapshot)
    assert committed == new_snapshot.revision
    assert (tmp_path / "CURRENT").read_text(encoding="ascii").strip() == committed
    reloaded = store.load("services/sink-realtime.yaml")
    assert reloaded.revision == committed


def test_generation_identifier_must_be_sha256(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path)
    (tmp_path / "CURRENT").write_text("not-a-sha256\n", encoding="ascii")
    with pytest.raises(ConfigurationError):
        store.load("services/sink-realtime.yaml")


def test_rejected_commit_leaves_current_untouched(tmp_path: Path) -> None:
    store, snapshot = _initial(tmp_path)
    before = (tmp_path / "CURRENT").read_text(encoding="ascii")
    docs = snapshot_docs(config_root=str(tmp_path))
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    new_snapshot = validate_snapshot(snapshot_files(docs))
    with pytest.raises(RevisionConflictError):
        store.commit(new_snapshot, "stale", old=snapshot)
    assert (tmp_path / "CURRENT").read_text(encoding="ascii") == before
    # The old generation still loads completely.
    assert store.load("services/sink-realtime.yaml").revision == snapshot.revision


def test_load_rejects_other_service_path_escape(tmp_path: Path) -> None:
    store, _ = _initial(tmp_path)
    with pytest.raises(ConfigurationError):
        store.load("../services/sink-realtime.yaml")
