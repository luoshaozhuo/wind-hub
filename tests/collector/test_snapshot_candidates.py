"""Snapshot candidate staging boundary tests."""

from __future__ import annotations

import pytest

from collector.infrastructure.config.snapshot_candidates import (
    SnapshotCandidateError,
    SnapshotCandidateStore,
)


@pytest.mark.parametrize(
    "path",
    [
        "../services/collector.yaml",
        "/tmp/collector.yaml",
        "instances/../services/collector.yaml",
        "instances//devices.yaml",
        "instances/./devices.yaml",
        "instances\\devices.yaml",
        "other/devices.yaml",
        "services/commander.yaml",
        "instances/deliveries.yaml",
        "instances/devices.json",
        "",
    ],
)
def test_rejects_unsafe_paths(path: str) -> None:
    store = SnapshotCandidateStore()
    with pytest.raises(SnapshotCandidateError):
        store.push(((path, b"schema_version: 1"),), base_revision="r1")


def test_push_preserves_original_bytes_without_mutating_active_files() -> None:
    store = SnapshotCandidateStore()
    files = (("instances/devices.yaml", b"schema_version: 1\n"),)
    candidate = store.push(files, base_revision="revision")
    assert store.get(candidate.candidate_id) == candidate
    assert candidate.files[0].content == files[0][1]
    assert candidate.files[0].sha256
    assert candidate.base_revision == "revision"


def test_rejects_duplicate_paths_and_capacity() -> None:
    store = SnapshotCandidateStore(max_candidates=1, max_files=2, max_total_bytes=4)
    with pytest.raises(SnapshotCandidateError, match="duplicate"):
        store.push((("instances/a.yaml", b"x"), ("instances/a.yaml", b"y")), base_revision="r1")
    with pytest.raises(SnapshotCandidateError, match="byte capacity"):
        store.push((("instances/a.yaml", b"12345"),), base_revision="r1")
    store.push((("instances/a.yaml", b"x"),), base_revision="r1")
    with pytest.raises(SnapshotCandidateError, match="capacity"):
        store.push((("instances/b.yaml", b"x"),), base_revision="r1")


def test_expiration_and_discard(monkeypatch: pytest.MonkeyPatch) -> None:
    from collector.infrastructure.config import snapshot_candidates

    now = [10.0]
    monkeypatch.setattr(snapshot_candidates.time, "monotonic", lambda: now[0])
    store = SnapshotCandidateStore(ttl_seconds=3)
    candidate = store.push((("instances/a.yaml", b"a"),), base_revision="r1")
    now[0] = 13.0
    with pytest.raises(SnapshotCandidateError, match="expired"):
        store.get(candidate.candidate_id)
    other = store.push((("instances/a.yaml", b"a"),), base_revision="r1")
    store.discard(other.candidate_id)
    with pytest.raises(SnapshotCandidateError):
        store.get(other.candidate_id)
