"""Collector real gRPC snapshot read and sole Repository publisher contract."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import grpc
import pytest

from collector.assembly import assemble_collector
from contracts import common_pb2 as pb
from contracts import common_pb2_grpc as rpc

pytestmark = [pytest.mark.component, pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[3]
AUTH = (("x-windhub-admin-token", "collector-component-token"),)


@pytest.mark.asyncio
async def test_full_snapshot_grpc_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", AUTH[0][1])
    for folder in ("services", "instances", "templates", "deployments"):
        shutil.copytree(ROOT / "configs" / folder, tmp_path / folder)
    service_file = tmp_path / "services" / "service_collector-realtime_v1.1.0.yaml"

    app = await assemble_collector(
        service_file,
        state_directory=tmp_path / "state",
        grpc_port=0,
    )
    try:
        await app.start()
        async with grpc.aio.insecure_channel(app.endpoint) as channel:
            client = rpc.CommonServiceStub(channel)
            info = await client.GetServiceInfo(pb.Empty())
            assert info.process_role == "collector"
            assert info.service_id == socket.gethostname()
            before = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK),
                metadata=AUTH,
            )
            assert before.revision and before.files
            names = {item.relative_path for item in before.files}
            assert "services/service_collector-realtime_v1.1.0.yaml" in names
            assert "instances/device_farm-a_v1.4.0.yaml" in names
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await client.PushConfigSnapshot(
                    pb.PushConfigSnapshotRequest(
                        expected_revision=before.revision,
                        files=[
                            pb.SnapshotFile(
                                relative_path=item.relative_path,
                                content=item.content,
                                size_bytes=len(item.content),
                            )
                            for item in before.files
                        ],
                    ),
                    metadata=AUTH,
                )
            assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await client.ApplyConfigSnapshot(
                    pb.ApplyConfigSnapshotRequest(
                        candidate_id="retired", expected_revision=before.revision
                    ),
                    metadata=AUTH,
                )
            assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            after = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK),
                metadata=AUTH,
            )
            runtime = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME),
                metadata=AUTH,
            )
            assert after.revision == before.revision
            assert runtime.revision == before.revision
    finally:
        await app.stop()
