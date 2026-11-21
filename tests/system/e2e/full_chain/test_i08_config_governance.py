"""I08：真实 Collector mTLS 配置候选的乐观锁冲突与不变性。"""

from __future__ import annotations

import grpc
import pytest

from cli.runtime import Runtime
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from tests.cli.test_r1f3_strict_cli_e2e import ADMIN_TOKEN

from .conftest import FullChainHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


def _files(snapshot: common.GetConfigSnapshotResponse) -> dict[str, bytes]:
    return {item.relative_path: bytes(item.content) for item in snapshot.files}


async def _snapshot(
    runtime: Runtime, stub: common_rpc.CommonServiceStub, scope: int
) -> common.GetConfigSnapshotResponse:
    return await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=scope),
        timeout=15,
        metadata=runtime.get_rpc_metadata("collector", "GetConfigSnapshot"),
    )


async def test_i08_collector_revision_conflict_preserves_disk_and_runtime(
    full_chain: FullChainHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    runtime = Runtime(full_chain.cli_rt, "json")
    try:
        common_stub = common_rpc.CommonServiceStub(runtime.channel("collector"))
        disk_before = await _snapshot(runtime, common_stub, common.SNAPSHOT_SCOPE_DISK)
        runtime_before = await _snapshot(runtime, common_stub, common.SNAPSHOT_SCOPE_RUNTIME)
        candidate = await common_stub.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(
                files=disk_before.files,
                expected_revision=disk_before.revision,
            ),
            timeout=30,
            metadata=runtime.get_rpc_metadata("collector", "PushConfigSnapshot"),
        )
        assert candidate.valid is True
        assert candidate.candidate_id

        with pytest.raises(grpc.aio.AioRpcError) as conflict_error:
            await common_stub.ApplyConfigSnapshot(
                common.ApplyConfigSnapshotRequest(
                    candidate_id=candidate.candidate_id,
                    expected_revision="0" * 64,
                ),
                timeout=30,
                metadata=runtime.get_rpc_metadata("collector", "ApplyConfigSnapshot"),
            )
        assert conflict_error.value.code() is grpc.StatusCode.ABORTED

        disk_after = await _snapshot(runtime, common_stub, common.SNAPSHOT_SCOPE_DISK)
        runtime_after = await _snapshot(runtime, common_stub, common.SNAPSHOT_SCOPE_RUNTIME)
        assert disk_after.revision == disk_before.revision
        assert runtime_after.revision == runtime_before.revision
        assert _files(disk_after) == _files(disk_before)
        assert _files(runtime_after) == _files(runtime_before)
        full_chain.record(
            "I08-COLLECTOR-REVISION-CONFLICT",
            candidate_id=candidate.candidate_id,
            expected_revision="0" * 64,
            disk_revision=disk_before.revision,
            runtime_revision=runtime_before.revision,
            cli_status="N-A: exercised through CommonService gRPC boundary",
            grpc_status=conflict_error.value.code().name,
            assertions={
                "precise_status_aborted": True,
                "disk_revision_and_files_unchanged": True,
                "runtime_revision_and_files_unchanged": True,
            },
        )
    finally:
        await runtime.close()
