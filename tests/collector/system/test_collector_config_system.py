"""UC-04 system tests: snapshot Push inertness, conflicts and commit semantics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts.common_pb2 import PushConfigSnapshotResponse
from contracts.common_pb2_grpc import CommonServiceStub
from tests.collector.system.conftest import ADMIN_METADATA, ClientsFactory
from tests.collector.system.helpers import disk_revision, push_apply, snapshot_files

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


async def _push(
    config_stub: CommonServiceStub, files: dict[str, bytes], revision: str
) -> PushConfigSnapshotResponse:
    response: PushConfigSnapshotResponse = await config_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[
                common.SnapshotFile(
                    relative_path=path,
                    content=content,
                    size_bytes=len(content),
                )
                for path, content in sorted(files.items())
            ],
            expected_revision=revision,
        ),
        metadata=ADMIN_METADATA,
    )
    return response


async def test_uc04_s01_push_is_inert_until_apply(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _channel, config_stub, _device_stub = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(config_stub)
    files = await snapshot_files(config_stub)

    site = yaml.safe_load(files["instances/site.yaml"])
    site["name"] = "Acceptance Push Staged"
    files["instances/site.yaml"] = yaml.safe_dump(site, sort_keys=False).encode()

    pushed = await _push(config_stub, files, revision)
    assert pushed.valid
    # A staged candidate never moves the committed generation by itself.
    assert await disk_revision(config_stub) == revision
    disk_site = yaml.safe_load((await snapshot_files(config_stub))["instances/site.yaml"])
    assert disk_site["name"] == "Acceptance Site"

    result = await push_apply(config_stub, files, revision)
    assert result.file_commit
    committed = await disk_revision(config_stub)
    assert committed != revision
    applied_site = yaml.safe_load((await snapshot_files(config_stub))["instances/site.yaml"])
    assert applied_site["name"] == "Acceptance Push Staged"


async def test_uc04_s02_stale_revision_and_published_template_immutability(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _channel, config_stub, _device_stub = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(config_stub)
    files = await snapshot_files(config_stub)

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await _push(config_stub, files, "0" * 64)
    assert exc.value.code() == grpc.StatusCode.ABORTED

    # A published, versioned template can never change in place. Mutate a
    # schema-legal option (slave_id, 0..255) so the content digest changes
    # without tripping the unknown-field rejection first; the immutable
    # published-version check must then refuse the snapshot.
    template_path = "templates/protocols/modbus_tcp_v1.0.0.yaml"
    mutated = yaml.safe_load(files[template_path])
    options = dict(mutated.get("protocol_options", {}))
    options["slave_id"] = 247
    mutated["protocol_options"] = options
    files[template_path] = yaml.safe_dump(mutated, sort_keys=False).encode()
    rejected = await _push(config_stub, files, revision)
    assert not rejected.valid
    assert "published template version" in rejected.validation[0].message
    assert await disk_revision(config_stub) == revision

    # Dropping a required file fails validation atomically; CURRENT stays put.
    incomplete = {path: content for path, content in files.items() if "device_models" not in path}
    rejected = await _push(config_stub, incomplete, revision)
    assert not rejected.valid
    assert await disk_revision(config_stub) == revision


async def test_uc04_s03_service_document_change_requires_restart(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _channel, config_stub, _device_stub = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(config_stub)
    files = await snapshot_files(config_stub)

    service = yaml.safe_load(files["services/collector.yaml"])
    service["runtime"]["connect_timeout"] = 4
    files["services/collector.yaml"] = yaml.safe_dump(service, sort_keys=False).encode()
    result = await push_apply(config_stub, files, revision)
    assert result.restart_required
    assert any(item.status == "RESTART_REQUIRED" for item in result.apply_results)

    # Until the process restarts, RUNTIME keeps the old service document.
    runtime = await config_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME),
        metadata=ADMIN_METADATA,
    )
    runtime_service = yaml.safe_load(
        next(
            item.content
            for item in runtime.files
            if item.relative_path == "services/collector.yaml"
        )
    )
    assert runtime_service["runtime"]["connect_timeout"] == 3

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id="deadbeef", expected_revision=result.file_commit
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION
