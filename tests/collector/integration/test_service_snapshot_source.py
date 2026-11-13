"""Real filesystem integration: service references feed immutable generations."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.snapshot_repository import CollectorSnapshotRepository
from collector.infrastructure.config.validation import resolve_source_paths

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_service_path_binding_persists_without_source_fallback(tmp_path: Path) -> None:
    config_root = tmp_path / "source"
    services = config_root / "services"
    instances = config_root / "custom"
    templates = config_root / "releases"
    for directory in (services, instances, templates):
        directory.mkdir(parents=True)
    service = services / "collector.yaml"
    service.write_text(
        yaml.safe_dump(
            {
                "config_sources": {
                    "instances": {
                        "site": "custom/site-A.yaml",
                        "devices": "custom/turbines-A.yaml",
                    },
                    "templates": {"root": "releases"},
                }
            }
        ),
        encoding="utf-8",
    )
    (instances / "site-A.yaml").write_bytes(b"site_id: alpha\n")
    (instances / "turbines-A.yaml").write_bytes(b"devices: {}\n")
    (templates / "units_standard.yaml").write_bytes(b"quantities: {}\n")
    (templates / "business_points_standard.yaml").write_bytes(b"business_points: {}\n")
    paths = resolve_source_paths(service)
    repository = CollectorSnapshotRepository(tmp_path / "state")
    files = repository.collect_source(paths)
    assert files["instances/site.yaml"] == b"site_id: alpha\n"
    assert files["instances/devices.yaml"] == b"devices: {}\n"

    def validate(snapshot: object) -> None:
        assert snapshot is not None

    candidate = repository.stage(files, expected_revision="", validate=validate)
    committed = await repository.commit(
        candidate.candidate_id, expected_revision="", validate=validate
    )
    (instances / "site-A.yaml").write_bytes(b"site_id: changed\n")
    recovered = CollectorSnapshotRepository(tmp_path / "state")
    assert recovered.active_revision() == committed.revision
    assert recovered.read_generation()["instances/site.yaml"] == b"site_id: alpha\n"
