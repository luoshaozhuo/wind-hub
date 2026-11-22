"""Collector system configuration: read scopes and sole Repository publisher guard.

Version CAS, immutable-template checks and publication are owned by
Configuration Repository tests, not Collector's disabled snapshot writer RPCs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts.common_pb2_grpc import CommonServiceStub
from tests.collector.system.conftest import ADMIN_METADATA, ClientsFactory
from tests.collector.system.helpers import disk_revision, snapshot_files

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


async def _reject_push(
    stub: CommonServiceStub, files: dict[str, bytes], revision: str
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(
                files=[
                    common.SnapshotFile(
                        relative_path=name, content=data, size_bytes=len(data)
                    )
                    for name, data in sorted(files.items())
                ],
                expected_revision=revision,
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION


async def test_uc04_s01_push_is_inert_until_apply(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _, stub, _ = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(stub)
    original = await snapshot_files(stub)
    mutated = dict(original)
    site_path = "instances/site_site_alpha_v1.0.0.yaml"
    site = yaml.safe_load(mutated[site_path])
    site["name"] = "unauthorized edit"
    mutated[site_path] = yaml.safe_dump(site, sort_keys=False).encode()
    await _reject_push(stub, mutated, revision)
    assert await disk_revision(stub) == revision
    assert await snapshot_files(stub) == original
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id="not-published", expected_revision=revision
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION


async def test_uc04_s02_stale_revision_and_published_template_immutability(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _, stub, _ = grpc_clients(handle.process.grpc_port)
    revision = await disk_revision(stub)
    original = await snapshot_files(stub)
    tampered = dict(original)
    template = "templates/protocols/protocol_modbus_tcp_v1.0.0.yaml"
    document = yaml.safe_load(tampered[template])
    document["protocol_options"] = {"slave_id": 247}
    tampered[template] = yaml.safe_dump(document, sort_keys=False).encode()
    await _reject_push(stub, tampered, revision)
    await _reject_push(stub, original, "0" * 64)
    assert await disk_revision(stub) == revision
    assert await snapshot_files(stub) == original


async def test_uc04_s03_service_document_change_requires_restart(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _, stub, _ = grpc_clients(handle.process.grpc_port)
    disk = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
        metadata=ADMIN_METADATA,
    )
    runtime = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME),
        metadata=ADMIN_METADATA,
    )
    assert runtime.revision == disk.revision
    await _reject_push(
        stub, await snapshot_files(stub), disk.revision
    )
    again = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME),
        metadata=ADMIN_METADATA,
    )
    assert again.revision == runtime.revision
    assert again.files == runtime.files
