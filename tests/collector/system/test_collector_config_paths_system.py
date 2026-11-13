"""CFG-PATH-01..08: real-process v1_9 service source path contract."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts import common_pb2 as pb
from tests.collector.system.conftest import ADMIN_METADATA, REPO_ROOT, ClientsFactory
from tests.collector.system.helpers import disk_revision, snapshot_files

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


def _check(service: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "collector.main", "--check", "--config", str(service)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )


async def test_cfg_path_01_custom_source_paths(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector(
        site_ref="alternative/alpha.yaml",
        devices_ref="alternative/turbines.yaml",
        templates_ref="releases",
    )
    assert _check(handle.services / "collector.yaml").returncode == 0
    root = handle.process.config_root
    assert not (root / "instances" / "site.yaml").exists()
    assert not (root / "instances" / "devices.yaml").exists()
    assert not (root / "templates").exists()
    _, client, _ = grpc_clients(handle.process.grpc_port)
    files = await snapshot_files(client)
    assert "instances/site.yaml" in files
    assert "instances/devices.yaml" in files
    assert "templates/units_standard.yaml" in files


@pytest.mark.parametrize(
    "bad_reference",
    [
        {"instances": {"site": "instances/site.yaml", "devices": "instances/devices.yaml"}},
        {
            "instances": {"site": "instances/site.yaml", "devices": "instances/devices.yaml"},
            "templates": {"pathname": "templates"},
        },
        {
            "instances": {"site": "../outside.yaml", "devices": "instances/devices.yaml"},
            "templates": {"root": "templates"},
        },
        {
            "instances": {"site": "missing/site.yaml", "devices": "instances/devices.yaml"},
            "templates": {"root": "templates"},
        },
        {"instances": {"site": "instances/site.yaml"}, "templates": {"root": "templates"}},
    ],
)
async def test_cfg_path_02_03_08_reject_invalid_sources(
    collector: Callable[..., Any], bad_reference: dict[str, object]
) -> None:
    handle = await collector()
    service = handle.services / "collector.yaml"
    document = yaml.safe_load(service.read_text(encoding="utf-8"))
    document["config_sources"] = bad_reference
    service.write_text(yaml.safe_dump(document), encoding="utf-8")
    result = _check(service)
    assert result.returncode != 0


async def test_cfg_path_05_service_references_switch_only_after_restart(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _, client, _ = grpc_clients(handle.process.grpc_port)
    old_revision = await disk_revision(client)
    files = await snapshot_files(client)
    new_files = dict(files)
    service = yaml.safe_load(new_files["services/collector.yaml"])
    service["config_sources"]["instances"]["site"] = "other/site.yaml"
    new_files["services/collector.yaml"] = yaml.safe_dump(service).encode("utf-8")
    push = await client.PushConfigSnapshot(
        pb.PushConfigSnapshotRequest(
            expected_revision=old_revision,
            files=[
                pb.SnapshotFile(relative_path=name, content=value, size_bytes=len(value))
                for name, value in sorted(new_files.items())
            ],
        ),
        metadata=ADMIN_METADATA,
    )
    assert push.valid
    result = await client.ApplyConfigSnapshot(
        pb.ApplyConfigSnapshotRequest(
            candidate_id=push.candidate_id, expected_revision=old_revision
        ),
        metadata=ADMIN_METADATA,
    )
    assert result.restart_required
    assert result.file_commit != old_revision
    assert result.effective_revision == old_revision
    runtime = await client.GetConfigSnapshot(
        pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME),
        metadata=ADMIN_METADATA,
    )
    runtime_service = next(
        item.content for item in runtime.files if item.relative_path == "services/collector.yaml"
    )
    assert yaml.safe_load(runtime_service)["config_sources"]["instances"]["site"] == (
        "instances/site.yaml"
    )


async def test_cfg_path_06_restart_uses_committed_generation(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _, first_client, _ = grpc_clients(handle.process.grpc_port)
    first_revision = await disk_revision(first_client)
    handle.process.process.kill()
    assert handle.process.process.wait(timeout=10) != 0
    restarted = await collector(
        state_dir_override=str(handle.state_dir),
        site_ref="replacement/site.yaml",
        devices_ref="replacement/devices.yaml",
        templates_ref="replacement_templates",
    )
    _, second_client, _ = grpc_clients(restarted.process.grpc_port)
    assert await disk_revision(second_client) == first_revision
    files = await snapshot_files(second_client)
    assert "instances/site.yaml" in files
    assert "templates/units_standard.yaml" in files


async def test_cfg_path_04_push_apply_custom_named_source_snapshot(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector(
        site_ref="custom/site-A.yaml",
        devices_ref="custom/devices-A.yaml",
        templates_ref="custom_templates",
    )
    _, client, _ = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(client)
    files = await snapshot_files(client)
    site = yaml.safe_load(files["instances/site.yaml"])
    site["name"] = "changed in candidate"
    files["instances/site.yaml"] = yaml.safe_dump(site).encode("utf-8")
    push = await client.PushConfigSnapshot(
        pb.PushConfigSnapshotRequest(
            expected_revision=revision,
            files=[
                pb.SnapshotFile(relative_path=name, content=data, size_bytes=len(data))
                for name, data in sorted(files.items())
            ],
        ),
        metadata=ADMIN_METADATA,
    )
    assert push.valid and push.candidate_id
    assert await disk_revision(client) == revision
    applied = await client.ApplyConfigSnapshot(
        pb.ApplyConfigSnapshotRequest(candidate_id=push.candidate_id, expected_revision=revision),
        metadata=ADMIN_METADATA,
    )
    assert applied.file_commit != revision
    active = await snapshot_files(client)
    assert yaml.safe_load(active["instances/site.yaml"])["name"] == "changed in candidate"


async def test_cfg_path_07_two_template_roots_never_fall_back(
    collector: Callable[..., Any],
) -> None:
    handle = await collector(templates_ref="releases/approved")
    root = handle.process.config_root
    alternate = root / "releases" / "approved"
    assert (alternate / "units_standard.yaml").is_file()
    default = root / "templates"
    default.mkdir()
    # A broken default repository must not affect the explicitly selected one.
    (default / "units_standard.yaml").write_bytes(b"invalid: [\n")
    result = _check(handle.services / "collector.yaml")
    assert result.returncode == 0, result.stdout + result.stderr
