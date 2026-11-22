"""Immutable Collector generation binds to explicitly selected source versions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.snapshot_repository import CollectorSnapshotRepository
from collector.infrastructure.config.validation import resolve_source_paths
from tests.collector.system.config_tree import write_config_tree

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_service_path_binding_persists_without_source_fallback(tmp_path: Path) -> None:
    root = tmp_path / "source"
    services = write_config_tree(
        root,
        modbus_port=1502,
        iec104_port=2404,
        nats_servers=["nats://127.0.0.1:4222"],
        site_ref="site-A",
        devices_ref="turbines-A",
    )
    service = services / "service_collector-realtime_v1.0.0.yaml"
    paths = resolve_source_paths(service)
    repository = CollectorSnapshotRepository(tmp_path / "state")
    files = repository.collect_source(paths)
    site_path = "instances/site_site-A_v1.0.0.yaml"
    device_path = "instances/device_turbines-A_v1.0.0.yaml"
    assert site_path in files
    assert device_path in files
    original = files[site_path]

    def validate(snapshot: object) -> None:
        assert snapshot is not None

    candidate = repository.stage(files, expected_revision="", validate=validate)
    committed = await repository.commit(
        candidate.candidate_id, expected_revision="", validate=validate
    )
    site = yaml.safe_load(paths.site.read_text(encoding="utf-8"))
    site["site_id"] = "changed"
    paths.site.write_text(yaml.safe_dump(site), encoding="utf-8")
    recovered = CollectorSnapshotRepository(tmp_path / "state")
    assert recovered.active_revision() == committed.revision
    assert recovered.read_generation()[site_path] == original
