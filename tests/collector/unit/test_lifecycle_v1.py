"""Lifecycle transition and runtime model ownership tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from collector.application.config_models import SnapshotPaths
from collector.application.lifecycle import (
    CollectorLifecycle,
    ConfiguredRuntime,
    Lifecycle,
)

pytestmark = pytest.mark.unit


def _paths(tmp_path: Path) -> SnapshotPaths:
    return SnapshotPaths(tmp_path / "collector.yaml", tmp_path, tmp_path)


@pytest.mark.asyncio
async def test_lifecycle_start_update_stop(tmp_path: Path) -> None:
    calls: list[str] = []

    def parse(paths: SnapshotPaths, revision: str) -> ConfiguredRuntime:
        assert paths.service.name == "collector.yaml"
        calls.append(revision)
        return ConfiguredRuntime(revision, "site", ("device-1",))

    runtime = CollectorLifecycle(parse)
    assert runtime.state is Lifecycle.NEW
    await runtime.start(_paths(tmp_path), "r1")
    running: Lifecycle = runtime.state
    assert running is Lifecycle.RUNNING
    assert runtime.configured == ConfiguredRuntime("r1", "site", ("device-1",))
    assert await runtime.converge(_paths(tmp_path), "r2") == {}
    assert runtime.configured is not None
    assert runtime.configured.revision == "r2"
    await runtime.stop()
    stopped_after_run: Lifecycle = runtime.state
    assert stopped_after_run is Lifecycle.STOPPED
    assert runtime.configured is None
    assert calls == ["r1", "r2"]


@pytest.mark.asyncio
async def test_failed_reconfiguration_keeps_previous_model(tmp_path: Path) -> None:
    def parse(_paths: SnapshotPaths, revision: str) -> ConfiguredRuntime:
        if revision == "bad":
            raise ValueError("invalid candidate")
        return ConfiguredRuntime(revision, "site", ())

    runtime = CollectorLifecycle(parse)
    await runtime.start(_paths(tmp_path), "good")
    assert await runtime.converge(_paths(tmp_path), "bad") == {"collector": "invalid candidate"}
    assert runtime.configured == ConfiguredRuntime("good", "site", ())
    assert runtime.state is Lifecycle.RUNNING


@pytest.mark.asyncio
async def test_startup_failure_is_not_reported_as_running(tmp_path: Path) -> None:
    def invalid(_paths: SnapshotPaths, _revision: str) -> ConfiguredRuntime:
        raise ValueError("invalid startup")

    runtime = CollectorLifecycle(invalid)
    with pytest.raises(ValueError, match="invalid startup"):
        await runtime.start(_paths(tmp_path), "bad")
    assert runtime.state is Lifecycle.FAILED
    await runtime.stop()
    stopped: Lifecycle = runtime.state
    assert stopped is Lifecycle.STOPPED
