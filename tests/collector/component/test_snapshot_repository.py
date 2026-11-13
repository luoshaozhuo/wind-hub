"""Collector immutable snapshot generation tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from collector.infrastructure.config.snapshot_repository import (
    CollectorSnapshotRepository,
    SnapshotConflictError,
    SnapshotError,
    SnapshotPaths,
)

pytestmark = pytest.mark.component


def _files() -> dict[str, bytes]:
    return {
        "services/collector.yaml": b"schema_version: 1\n",
        "instances/site.yaml": b"schema_version: 1\n",
        "instances/devices.yaml": b"schema_version: 1\n",
        "templates/units_standard.yaml": b"schema_version: 1\n",
        "templates/business_points_standard.yaml": b"schema_version: 1\n",
    }


def _validate(paths: SnapshotPaths) -> None:
    assert paths.service.is_file()
    assert (paths.instances / "devices.yaml").is_file()
    assert (paths.templates / "units_standard.yaml").is_file()


@pytest.mark.asyncio
async def test_candidate_is_inert_until_commit(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")
    candidate = repository.stage(_files(), expected_revision="", validate=_validate)
    assert repository.active_revision() is None
    committed = await repository.commit(
        candidate.candidate_id, expected_revision="", validate=_validate
    )
    assert repository.active_revision() == committed.revision
    assert repository.active_paths() == committed.paths


@pytest.mark.asyncio
async def test_old_generation_remains_readable_after_switch(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")
    first = repository.stage(_files(), expected_revision="", validate=_validate)
    old = await repository.commit(first.candidate_id, expected_revision="", validate=_validate)
    modified = _files()
    modified["instances/devices.yaml"] = b"schema_version: 2\n"
    second = repository.stage(modified, expected_revision=old.revision, validate=_validate)
    current = await repository.commit(
        second.candidate_id, expected_revision=old.revision, validate=_validate
    )
    assert old.paths.instances.joinpath("devices.yaml").read_bytes() == b"schema_version: 1\n"
    assert current.paths.instances.joinpath("devices.yaml").read_bytes() == b"schema_version: 2\n"


@pytest.mark.asyncio
async def test_stale_candidate_does_not_overwrite_active(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")
    first = repository.stage(_files(), expected_revision="", validate=_validate)
    stale = repository.stage(_files(), expected_revision="", validate=_validate)
    committed = await repository.commit(
        first.candidate_id, expected_revision="", validate=_validate
    )
    with pytest.raises(SnapshotConflictError):
        await repository.commit(stale.candidate_id, expected_revision="", validate=_validate)
    assert repository.active_revision() == committed.revision


@pytest.mark.parametrize(
    "invalid",
    [
        "/absolute.yaml",
        "../services/collector.yaml",
        "services/commander.yaml",
        "instances/deliveries.yaml",
        "templates/deliveries/a/1.0.0.yaml",
        "templates/point_tables/point_table_../tasks/1.0.0.yaml",
        "instances\\devices.yaml",
    ],
)
def test_forbids_paths_outside_collector_ownership(tmp_path: Path, invalid: str) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")
    files = _files()
    files[invalid] = b"bad"
    with pytest.raises(SnapshotError):
        repository.stage(files, expected_revision="", validate=_validate)
    assert repository.active_revision() is None


def test_core_validation_failure_keeps_current_pointer(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")

    def reject(_paths: SnapshotPaths) -> None:
        raise ValueError("schema violation")

    with pytest.raises(ValueError, match="schema violation"):
        repository.stage(_files(), expected_revision="", validate=reject)
    assert repository.active_revision() is None


def test_candidate_size_is_bounded(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config", max_bytes=20)
    with pytest.raises(SnapshotError, match="byte limit"):
        repository.stage(_files(), expected_revision="", validate=_validate)


@pytest.mark.asyncio
async def test_published_template_version_cannot_change(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")
    original = _files()
    original["templates/tasks/demo/1.0.0.yaml"] = b"interval: 1\n"
    first = repository.stage(original, expected_revision="", validate=_validate)
    committed = await repository.commit(
        first.candidate_id, expected_revision="", validate=_validate
    )
    modified = dict(original)
    modified["templates/tasks/demo/1.0.0.yaml"] = b"interval: 2\n"
    with pytest.raises(SnapshotError, match="published template"):
        repository.stage(modified, expected_revision=committed.revision, validate=_validate)
    modified.pop("templates/tasks/demo/1.0.0.yaml")
    with pytest.raises(SnapshotError, match="published template"):
        repository.stage(modified, expected_revision=committed.revision, validate=_validate)


@pytest.mark.asyncio
async def test_commit_receipt_survives_repository_recreation(tmp_path: Path) -> None:
    root = tmp_path / "config"
    first = CollectorSnapshotRepository(root)
    candidate = first.stage(_files(), expected_revision="", validate=_validate)
    result = await first.commit(candidate.candidate_id, expected_revision="", validate=_validate)
    recovered = CollectorSnapshotRepository(root)
    assert recovered.committed_revision(candidate.candidate_id, "") == result.revision
    replay = await recovered.commit(
        candidate.candidate_id, expected_revision="", validate=_validate
    )
    assert replay.revision == result.revision
    assert recovered.active_revision() == result.revision


@pytest.mark.asyncio
async def test_prepared_receipt_recovers_after_pointer_switch(tmp_path: Path) -> None:
    root = tmp_path / "config"
    repository = CollectorSnapshotRepository(root)
    candidate = repository.stage(_files(), expected_revision="", validate=_validate)
    committed = await repository.commit(
        candidate.candidate_id, expected_revision="", validate=_validate
    )
    repository._write_receipt(candidate.candidate_id, "", committed.revision, state="PREPARED")
    reopened = CollectorSnapshotRepository(root)
    assert reopened.committed_revision(candidate.candidate_id, "") == committed.revision


@pytest.mark.asyncio
async def test_slow_disk_commit_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "config")
    candidate = repository.stage(_files(), expected_revision="", validate=_validate)
    running = threading.Event()
    proceed = threading.Event()
    original = repository._write_generation

    def slow_write(root: Path, files: tuple[tuple[str, bytes], ...]) -> None:
        running.set()
        if not proceed.wait(timeout=5):
            raise TimeoutError("test fixture timed out")
        original(root, files)

    monkeypatch.setattr(repository, "_write_generation", slow_write)
    operation = asyncio.create_task(
        repository.commit(candidate.candidate_id, expected_revision="", validate=_validate)
    )
    try:
        assert await asyncio.to_thread(running.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0), 0.5)
        assert not operation.done()
    finally:
        proceed.set()
    committed = await operation
    assert repository.active_revision() == committed.revision


@pytest.mark.asyncio
async def test_candidate_survives_process_restart(tmp_path: Path) -> None:
    root = tmp_path / "config"
    first = CollectorSnapshotRepository(root)
    staged = first.stage(_files(), expected_revision="", validate=_validate)
    second = CollectorSnapshotRepository(root)
    recovered = second.get(staged.candidate_id)
    assert dict(recovered.files) == _files()
    result = await second.commit(staged.candidate_id, expected_revision="", validate=_validate)
    assert second.active_revision() == result.revision


@pytest.mark.asyncio
async def test_gc_preserves_active_and_receipted_generations(tmp_path: Path) -> None:
    root = tmp_path / "config"
    repository = CollectorSnapshotRepository(root)
    first = repository.stage(_files(), expected_revision="", validate=_validate)
    committed = await repository.commit(
        first.candidate_id, expected_revision="", validate=_validate
    )
    extra = root / "generations" / ("a" * 64)
    extra.mkdir()
    (extra / "orphan.yaml").write_text("orphan", encoding="utf-8")
    assert repository.collect_orphan_generations() == 1
    assert (root / "generations" / committed.revision).is_dir()
    assert not extra.exists()


@pytest.mark.asyncio
async def test_stage_reclaims_orphan_generations_from_crash_window(tmp_path: Path) -> None:
    root = tmp_path / "config"
    repository = CollectorSnapshotRepository(root)
    first = repository.stage(_files(), expected_revision="", validate=_validate)
    committed = await repository.commit(
        first.candidate_id, expected_revision="", validate=_validate
    )
    # Simulate a crash in the window between generation promotion and the
    # durable receipt: the directory exists but nothing references it.
    orphan = root / "generations" / ("b" * 64)
    orphan.mkdir()
    (orphan / "leftover.yaml").write_text("leftover", encoding="utf-8")
    files = dict(_files())
    files["services/collector.yaml"] = b"schema_version: 2\n"
    next_candidate = repository.stage(
        files, expected_revision=committed.revision, validate=_validate
    )
    assert not orphan.exists()
    assert (root / "generations" / committed.revision).is_dir()
    assert next_candidate.base_revision == committed.revision
