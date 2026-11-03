"""Collector stage-one config RPC and runtime lifecycle component test."""

from __future__ import annotations

import shutil
from pathlib import Path

import grpc
import pytest
import yaml

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
    for folder in ("services", "instances", "templates"):
        shutil.copytree(ROOT / "configs" / folder, tmp_path / folder)
    service_file = tmp_path / "services" / "collector.yaml"
    service = yaml.safe_load(service_file.read_text(encoding="utf-8"))
    service["file_version"] = "1.0.0"
    service_file.write_text(yaml.safe_dump(service, allow_unicode=True), encoding="utf-8")

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
            assert info.service_id == "collector"
            before = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK),
                metadata=AUTH,
            )
            assert before.revision and before.files
            original = {item.relative_path: bytes(item.content) for item in before.files}
            modified = dict(original)
            service = yaml.safe_load(modified["services/collector.yaml"])
            service["watchdog"]["interval"] = 6
            modified["services/collector.yaml"] = yaml.safe_dump(service).encode("utf-8")
            push = await client.PushConfigSnapshot(
                pb.PushConfigSnapshotRequest(
                    expected_revision=before.revision,
                    files=[
                        pb.SnapshotFile(relative_path=name, content=data, size_bytes=len(data))
                        for name, data in sorted(modified.items())
                    ],
                ),
                metadata=AUTH,
            )
            assert push.valid and push.candidate_id
            still_old = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK),
                metadata=AUTH,
            )
            assert still_old.revision == before.revision
            request = pb.ApplyConfigSnapshotRequest(
                candidate_id=push.candidate_id,
                expected_revision=before.revision,
            )
            applied = await client.ApplyConfigSnapshot(request, metadata=AUTH)
            assert applied.restart_required
            assert applied.file_commit != before.revision
            assert applied.effective_revision == before.revision
            runtime = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME),
                metadata=AUTH,
            )
            assert runtime.revision == before.revision
            current = await client.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK),
                metadata=AUTH,
            )
            assert current.revision == applied.file_commit
            repeat = await client.ApplyConfigSnapshot(request, metadata=AUTH)
            assert repeat.file_commit == applied.file_commit
            assert repeat.application_id == applied.application_id
    finally:
        await app.stop()
