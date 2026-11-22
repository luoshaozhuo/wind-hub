"""Real-process acceptance of exact Deployment selection and sole publisher guard."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from contracts import common_pb2 as pb
from tests.collector.system.conftest import ADMIN_METADATA, REPO_ROOT, ClientsFactory
from tests.collector.system.helpers import disk_revision, snapshot_files

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]

SERVICE_NAME = "service_collector-realtime_v1.0.0.yaml"
SITE_NAME = "instances/site_site_alpha_v1.0.0.yaml"


def _check(service: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "collector.main", "--check", "--config", str(service)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


async def test_cfg_path_01_custom_source_paths(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector(site_ref="alternate_site", devices_ref="alternate_devices")
    service = handle.services / SERVICE_NAME
    assert _check(service).returncode == 0
    _, client, _ = grpc_clients(handle.process.grpc_port)
    files = await snapshot_files(client)
    assert "instances/site_alternate_site_v1.0.0.yaml" in files
    assert "instances/device_alternate_devices_v1.0.0.yaml" in files
    assert "templates/units_standard_v1.2.0.yaml" in files


@pytest.mark.parametrize(
    "bad_reference",
    [
        {"instances": {"site": "site_alpha", "devices": "farm-a"}},
        {
            "instances": {
                "site": "site_alpha", "devices": "farm-a", "tasks": "farm-a"
            },
            "templates": {"root": "templates"},
        },
        {
            "instances": {
                "site": "../escape", "devices": "farm-a", "tasks": "farm-a"
            }
        },
        {
            "instances": {
                "site": "missing", "devices": "farm-a", "tasks": "farm-a"
            }
        },
        {"instances": {"site": "site_alpha", "tasks": "farm-a"}},
    ],
)
async def test_cfg_path_02_03_08_reject_invalid_sources(
    collector: Callable[..., Any], bad_reference: dict[str, object]
) -> None:
    handle = await collector()
    service = handle.services / SERVICE_NAME
    document = yaml.safe_load(service.read_text(encoding="utf-8"))
    document["config_sources"] = bad_reference
    service.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert _check(service).returncode != 0


async def test_cfg_path_05_service_references_switch_only_after_restart(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _, client, _ = grpc_clients(handle.process.grpc_port)
    initial = await disk_revision(client)
    original = await snapshot_files(client)
    service = handle.services / SERVICE_NAME
    document = yaml.safe_load(service.read_text(encoding="utf-8"))
    document["file_version"] = "1.0.1"
    document["config_sources"]["instances"]["site"] = "another_site"
    target = handle.services / "service_collector-realtime_v1.0.1.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    source_site = handle.instances / "site_site_alpha_v1.0.0.yaml"
    next_site = yaml.safe_load(source_site.read_text(encoding="utf-8"))
    next_site["site_id"] = "another_site"
    (handle.instances / "site_another_site_v1.0.0.yaml").write_text(
        yaml.safe_dump(next_site), encoding="utf-8"
    )
    manifest = handle.process.config_root / "deployments" / (
        "deployment_collector-acceptance_v1.0.0.yaml"
    )
    deployment = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    deployment["services"]["collector-realtime"]["file_version"] = "1.0.1"
    deployment["instances"]["site"]["another_site"] = {"file_version": "1.0.0"}
    manifest.write_text(yaml.safe_dump(deployment), encoding="utf-8")
    assert _check(target).returncode == 0
    assert _check(service).returncode != 0
    runtime = await client.GetConfigSnapshot(
        pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME),
        metadata=ADMIN_METADATA,
    )
    assert runtime.revision == initial
    assert SITE_NAME in original


async def test_cfg_path_06_restart_uses_committed_generation(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    first = await collector()
    _, client, _ = grpc_clients(first.process.grpc_port)
    initial_revision = await disk_revision(client)
    first.process.process.kill()
    assert first.process.process.wait(timeout=10) != 0
    restarted = await collector(state_dir_override=str(first.state_dir))
    _, current, _ = grpc_clients(restarted.process.grpc_port)
    assert await disk_revision(current) == initial_revision


async def test_cfg_path_04_push_apply_custom_named_source_snapshot(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector(site_ref="alternate_site", devices_ref="alternate_devices")
    _, client, _ = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(client)
    files = await snapshot_files(client)
    site_path = "instances/site_alternate_site_v1.0.0.yaml"
    site = yaml.safe_load(files[site_path])
    site["name"] = "Unauthorized write"
    files[site_path] = yaml.safe_dump(site).encode("utf-8")
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await client.PushConfigSnapshot(
            pb.PushConfigSnapshotRequest(
                expected_revision=revision,
                files=[
                    pb.SnapshotFile(
                        relative_path=name, content=data, size_bytes=len(data)
                    )
                    for name, data in sorted(files.items())
                ],
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert await disk_revision(client) == revision


async def test_cfg_path_07_two_template_roots_never_fall_back(
    collector: Callable[..., Any]
) -> None:
    handle = await collector()
    root = handle.process.config_root
    (root / "releases").mkdir()
    (root / "releases" / "units_standard_v1.2.0.yaml").write_bytes(b"invalid: [\n")
    assert _check(handle.services / SERVICE_NAME).returncode == 0
