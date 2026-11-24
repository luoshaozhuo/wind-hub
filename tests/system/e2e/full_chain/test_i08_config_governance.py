"""I08: Collector refuses local snapshot writes; Repository is sole publisher."""

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


async def test_i08_collector_local_publication_is_disabled(
    full_chain: FullChainHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    runtime = Runtime(full_chain.cli_rt, "json")
    try:
        stub = common_rpc.CommonServiceStub(runtime.channel("collector"))
        disk_before = await _snapshot(runtime, stub, common.SNAPSHOT_SCOPE_DISK)
        runtime_before = await _snapshot(runtime, stub, common.SNAPSHOT_SCOPE_RUNTIME)
        rejected: dict[str, str] = {}
        operations = (
            (
                "PushConfigSnapshot",
                common.PushConfigSnapshotRequest(
                    files=disk_before.files,
                    expected_revision=disk_before.revision,
                ),
            ),
            (
                "ApplyConfigSnapshot",
                common.ApplyConfigSnapshotRequest(
                    candidate_id="cannot-publish-outside-repository",
                    expected_revision=disk_before.revision,
                ),
            ),
        )
        for method, request in operations:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await getattr(stub, method)(
                    request,
                    timeout=30,
                    metadata=runtime.get_rpc_metadata("collector", method),
                )
            assert caught.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            rejected[method] = caught.value.code().name

        disk_after = await _snapshot(runtime, stub, common.SNAPSHOT_SCOPE_DISK)
        runtime_after = await _snapshot(runtime, stub, common.SNAPSHOT_SCOPE_RUNTIME)
        assert disk_after.revision == disk_before.revision
        assert runtime_after.revision == runtime_before.revision
        assert _files(disk_after) == _files(disk_before)
        assert _files(runtime_after) == _files(runtime_before)

        # Read-only CLI inspection remains permitted for the same Collector.
        readback = await full_chain.cli_call(
            full_chain.cli_rt, "config", "get", "collector", "--scope", "disk"
        )
        assert readback["status"] == "SUCCESS"
        full_chain.record(
            "I08-COLLECTOR-REPOSITORY-SOLE-PUBLISHER",
            rejected=rejected,
            disk_revision=disk_before.revision,
            runtime_revision=runtime_before.revision,
            disk_and_runtime_unchanged=True,
            readonly_cli_status=readback["status"],
        )
    finally:
        await runtime.close()
