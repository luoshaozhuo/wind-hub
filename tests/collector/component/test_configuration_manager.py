"""Configuration application contract tests for a new Collector runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from collector.application.configuration import CollectorConfigurationManager
from collector.infrastructure.config.snapshot_repository import (
    CollectorSnapshotRepository,
    SnapshotConflictError,
    SnapshotPaths,
)

pytestmark = pytest.mark.component


def _read_service(paths: SnapshotPaths) -> None:
    """Validate stand-in that succeeds when the service document is readable."""
    paths.service.read_bytes()


def _files() -> dict[str, bytes]:
    return {
        "services/collector.yaml": b"schema_version: 1\n",
        "instances/site.yaml": b"schema_version: 1\n",
        "instances/devices.yaml": b"schema_version: 1\n",
        "templates/units_standard.yaml": b"schema_version: 1\n",
        "templates/business_points_standard.yaml": b"schema_version: 1\n",
    }


class Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.seen_service: list[bytes] = []
        self.errors: dict[str, str] = {}

    async def converge(self, paths: SnapshotPaths, revision: str) -> dict[str, str]:
        assert paths.service.is_file()
        self.seen_service.append(paths.service.read_bytes())
        self.calls.append(revision)
        return dict(self.errors)


@pytest.mark.asyncio
async def test_push_is_inert_and_apply_activates(tmp_path: Path) -> None:
    runtime = Runtime()
    repository = CollectorSnapshotRepository(tmp_path / "cfg")
    manager = CollectorConfigurationManager(
        repository,
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda before, after: False,
    )
    identifier, base = manager.push(_files(), expected_revision="")
    assert base == ""
    assert manager.status().disk_revision == ""
    assert manager.get_candidate(identifier) == _files()
    status = await manager.apply(identifier, expected_revision="")
    assert status.converged
    assert status.disk_revision == runtime.calls[0]


@pytest.mark.asyncio
async def test_failed_runtime_convergence_is_reported(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.errors["device-1"] = "device session did not stop"
    manager = CollectorConfigurationManager(
        CollectorSnapshotRepository(tmp_path / "cfg"),
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda before, after: False,
    )
    identifier, _ = manager.push(_files(), expected_revision="")
    result = await manager.apply(identifier, expected_revision="")
    assert result.disk_revision
    assert result.runtime_revision == ""
    assert result.apply_errors == runtime.errors
    assert not result.converged


@pytest.mark.asyncio
async def test_restart_required_does_not_claim_runtime_activation(tmp_path: Path) -> None:
    runtime = Runtime()
    manager = CollectorConfigurationManager(
        CollectorSnapshotRepository(tmp_path / "cfg"),
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda before, after: before != after,
    )
    identifier, _ = manager.push(_files(), expected_revision="")
    first = await manager.apply(identifier, expected_revision="")
    modified = _files()
    modified["services/collector.yaml"] = b"schema_version: 2\n"
    identifier, _ = manager.push(modified, expected_revision=first.disk_revision)
    second = await manager.apply(identifier, expected_revision=first.disk_revision)
    assert second.restart_required
    assert second.runtime_revision == first.disk_revision
    assert len(runtime.calls) == 2
    assert runtime.seen_service == [
        _files()["services/collector.yaml"],
        _files()["services/collector.yaml"],
    ]
    assert runtime.calls[-1] == second.runtime_revision
    assert manager.get_runtime()["services/collector.yaml"] == _files()["services/collector.yaml"]
    assert not second.converged


@pytest.mark.asyncio
async def test_stale_apply_rejected_without_overwrite(tmp_path: Path) -> None:
    runtime = Runtime()
    manager = CollectorConfigurationManager(
        CollectorSnapshotRepository(tmp_path / "cfg"),
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda before, after: False,
    )
    first, _ = manager.push(_files(), expected_revision="")
    stale, _ = manager.push(_files(), expected_revision="")
    await manager.apply(first, expected_revision="")
    with pytest.raises(SnapshotConflictError):
        await manager.apply(stale, expected_revision="")


@pytest.mark.asyncio
async def test_repeated_apply_reuses_original_result(tmp_path: Path) -> None:
    runtime = Runtime()
    manager = CollectorConfigurationManager(
        CollectorSnapshotRepository(tmp_path / "cfg"),
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda before, after: False,
    )
    identifier, _ = manager.push(_files(), expected_revision="")
    first = await manager.apply(identifier, expected_revision="")
    replay = await manager.apply(identifier, expected_revision="")
    assert replay == first
    assert runtime.calls == [first.disk_revision]
    with pytest.raises(SnapshotConflictError):
        await manager.apply(identifier, expected_revision="incorrect")


@pytest.mark.asyncio
async def test_failed_apply_converges_later_without_recommitting(tmp_path: Path) -> None:
    runtime = Runtime()
    runtime.errors["device-1"] = "temporary device stop failure"
    manager = CollectorConfigurationManager(
        CollectorSnapshotRepository(tmp_path / "cfg"),
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda old, new: old != new,
    )
    candidate, _ = manager.push(_files(), expected_revision="")
    failed = await manager.apply(candidate, expected_revision="")
    assert failed.apply_errors
    assert failed.runtime_revision == ""
    runtime.errors.clear()
    recovered = await manager.retry_failed()
    assert recovered.converged
    assert recovered.disk_revision == failed.disk_revision
    assert runtime.calls == [failed.disk_revision, failed.disk_revision]
    assert await manager.apply(candidate, expected_revision="") == failed


@pytest.mark.asyncio
async def test_service_source_change_waits_for_restart(tmp_path: Path) -> None:
    """A source selection change cannot hot-load candidate instance contents."""
    runtime = Runtime()
    manager = CollectorConfigurationManager(
        CollectorSnapshotRepository(tmp_path / "state"),
        validate=_read_service,
        runtime=runtime,
        restart_required=lambda before, after: before != after,
    )
    original = _files()
    original["services/collector.yaml"] = (
        b"config_sources:\n"
        b"  instances:\n"
        b"    site: instances/site.yaml\n"
        b"    devices: instances/devices.yaml\n"
        b"  templates:\n"
        b"    root: templates\n"
    )
    candidate, _ = manager.push(original, expected_revision="")
    first = await manager.apply(candidate, expected_revision="")
    assert len(runtime.calls) == 1

    altered = dict(original)
    altered["services/collector.yaml"] = original["services/collector.yaml"].replace(
        b"instances/site.yaml", b"custom/site.yaml"
    )
    altered["instances/site.yaml"] = b"site_id: new_site\n"
    candidate, _ = manager.push(altered, expected_revision=first.disk_revision)
    result = await manager.apply(candidate, expected_revision=first.disk_revision)
    assert result.restart_required
    assert result.runtime_revision == first.runtime_revision
    assert len(runtime.calls) == 1
    assert manager.get_runtime() == original
    assert manager.get_disk()[1] == altered
