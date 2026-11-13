"""Durable task-intent updates through Collector snapshot transactions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.application.config_models import SnapshotConflictError, SnapshotPaths
from collector.application.configuration import CollectorConfigurationManager
from collector.infrastructure.config.snapshot_repository import CollectorSnapshotRepository
from collector.infrastructure.config.task_control import CollectorTaskControl

pytestmark = [pytest.mark.component, pytest.mark.contract]


def _read_service(paths: SnapshotPaths) -> None:
    """Validate stand-in that succeeds when the service document is readable."""
    paths.service.read_bytes()


class Runtime:
    async def converge(self, paths: SnapshotPaths, revision: str) -> dict[str, str]:
        assert paths.service.is_file()
        return {}


def _files() -> dict[str, bytes]:
    return {
        "services/collector.yaml": b"schema_version: 1\n",
        "instances/site.yaml": b"schema_version: 1\n",
        "instances/devices.yaml": yaml.safe_dump(
            {
                "schema_version": 1,
                "devices": {
                    "d1": {
                        "model": {"id": "example", "version": "1.0.0"},
                        "endpoint": {"host": "127.0.0.1"},
                    }
                },
            }
        ).encode(),
        "templates/units_standard.yaml": b"schema_version: 1\n",
        "templates/business_points_standard.yaml": b"schema_version: 1\n",
    }


@pytest.mark.asyncio
async def test_start_stop_persist_only_instance_override(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "state")
    manager = CollectorConfigurationManager(
        repository,
        validate=_read_service,
        runtime=Runtime(),
        restart_required=lambda old, new: old != new,
    )
    candidate, _ = manager.push(_files(), expected_revision="")
    initial = await manager.apply(candidate, expected_revision="")
    manager.mark_started()
    control = CollectorTaskControl(manager)
    started = await control.set_desired("d1", "START", expected_revision=initial.disk_revision)
    assert started.converged
    disk, files = manager.get_disk()
    document = yaml.safe_load(files["instances/devices.yaml"])
    assert document["devices"]["d1"]["overrides"]["task"]["desired_state"] == "START"
    assert not any(path.startswith("templates/tasks/") for path in files)
    stopped = await control.set_desired("d1", "STOP", expected_revision=disk)
    assert stopped.converged
    _, files = manager.get_disk()
    document = yaml.safe_load(files["instances/devices.yaml"])
    assert document["devices"]["d1"]["overrides"]["task"]["desired_state"] == "STOP"


@pytest.mark.asyncio
async def test_stale_start_stop_revision_rejected(tmp_path: Path) -> None:
    repository = CollectorSnapshotRepository(tmp_path / "state")
    manager = CollectorConfigurationManager(
        repository,
        validate=_read_service,
        runtime=Runtime(),
        restart_required=lambda old, new: old != new,
    )
    candidate, _ = manager.push(_files(), expected_revision="")
    initial = await manager.apply(candidate, expected_revision="")
    control = CollectorTaskControl(manager)
    await control.set_desired("d1", "START", expected_revision=initial.disk_revision)
    with pytest.raises(SnapshotConflictError):
        await control.set_desired("d1", "STOP", expected_revision=initial.disk_revision)
