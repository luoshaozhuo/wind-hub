"""Persisted Sink generation selection and Worker recovery regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.config_io import validate_snapshot
from sink_service.infrastructure.persistence.config_repository import GenerationStore
from tests.sink_service._stage1_fixtures import (
    SERVICE_FILE,
    snapshot_docs,
    snapshot_files,
)

pytestmark = [pytest.mark.component, pytest.mark.contract]


def _committed(root: Path) -> tuple[GenerationStore, str]:
    snapshot = validate_snapshot(snapshot_files(snapshot_docs(config_root=str(root))))
    store = GenerationStore(root)
    assert store.commit(snapshot, "", old=None) == snapshot.revision
    return store, snapshot.revision


def test_restart_uses_committed_versioned_service_path(tmp_path: Path) -> None:
    store, revision = _committed(tmp_path)
    assert store.selected_service_path() == SERVICE_FILE
    assert store.load(store.selected_service_path()).revision == revision
    assert store.load_generation(revision, SERVICE_FILE).revision == revision


def test_worker_recovery_rejects_unknown_generation(tmp_path: Path) -> None:
    store, revision = _committed(tmp_path)
    assert store.load_generation(revision, SERVICE_FILE).revision == revision
    with pytest.raises(ConfigurationError, match="missing or unsafe"):
        store.load_generation("0" * 64, SERVICE_FILE)


@pytest.mark.parametrize(
    "path",
    [
        "configs/services/../evil.yaml",
        "configs/services/sink_service.yaml",
        "/configs/services/service_sink-west_v1.0.0.yaml",
    ],
)
def test_worker_recovery_rejects_unsafe_or_unversioned_paths(tmp_path: Path, path: str) -> None:
    store, revision = _committed(tmp_path)
    with pytest.raises(ConfigurationError):
        store.load_generation(revision, path)
