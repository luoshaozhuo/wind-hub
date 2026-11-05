"""频繁 Apply（churn）期间的数据面连续性与租约清理。

交替 Apply gw1 desired_state STOP/START（真实监听器反复释放/重绑），同时：
- gw2 从未变更，并发读/写全程不受影响；
- gw1 每次重建后租约全部失效，旧 token 被 fencing；
- gw1 恢复 START 后写入与 Modbus 读恢复正常；
- 最终服务状态 HEALTHY，无 FAILED 残留。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as pb
from core.infrastructure.rpc.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_UINT,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service]


def _snapshot_file(relative_path: str, content: bytes) -> common.SnapshotFile:
    return common.SnapshotFile(
        relative_path=relative_path,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def _disk(stub) -> tuple[str, bytes, str]:
    reply = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
    )
    return reply.revision, reply.files[0].content, reply.files[0].relative_path


def _set_gw1_state(content: bytes, state: str) -> bytes:
    document = yaml.safe_load(content)
    for server in document["servers"]:
        if server["server_id"] == "gw1":
            server["desired_state"] = state
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()


async def _apply_state(harness, state: str) -> str:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk(stub)
    candidate = await stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[_snapshot_file(relative_path, _set_gw1_state(content, state))],
            expected_revision=revision,
        )
    )
    assert candidate.valid, candidate.validation
    applied = await stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=candidate.candidate_id,
            expected_revision=revision,
        )
    )
    assert not applied.restart_required
    return applied.effective_revision


async def test_repeated_apply_churn_preserves_sibling_and_clears_claims(
    tmp_path: Path,
) -> None:
    harness = await start_gateway_v1(tmp_path / "root", two_server_specs())
    stop_writes: list[bool] = []
    try:
        gw_stub = harness.gateway_stub()
        claim = await gw_stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id="churn",
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=60000,
            )
        )
        assert claim.accepted

        async def hammer_gw2(seq: int) -> None:
            reply = await gw_stub.UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw2",
                    values=[
                        pb.VariableUpdate(
                            variable_id=FEEDBACK_UINT,
                            value=encode_scalar(seq % 60000),
                        )
                    ],
                )
            )
            stop_writes.append(reply.results[0].success)

        for round_index, state in enumerate(("STOP", "START", "STOP", "START"), start=1):
            # 每轮 churn 同时对未变更的 gw2 施压。
            await asyncio.gather(
                _apply_state(harness, state),
                *(hammer_gw2(round_index * 10 + i) for i in range(8)),
            )
            await asyncio.sleep(0.1)

        # gw2 全部写入成功（重建 gw1 不影响兄弟监听器）。
        assert len(stop_writes) == 32
        assert all(stop_writes)
        readback = await gw_stub.ReadVariables(
            pb.ReadVariablesRequest(gateway_id="gw2", variable_ids=[FEEDBACK_UINT])
        )
        assert readback.values[0].quality in {"GOOD", "good"}

        # gw1 经历重建：首次 STOP 即清空租约，旧 token 最终必须被 fencing。
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await gw_stub.UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw1",
                    lease_token=claim.lease_token,
                    values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(1))],
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION

        # gw1 最终为 START/RUNNING：无 token 写入恢复正常。
        info = await gw_stub.GetGateway(common.ObjectId(id="gw1"))
        assert info.gateway.actual_state == "RUNNING"
        reply = await gw_stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(123))],
            )
        )
        assert reply.results[0].success

        gateway_deps: dict[str, str] = {}
        for _ in range(30):
            status = await harness.common_stub().GetServiceStatus(common.Empty())
            gateway_deps = {
                dep.name: dep.health for dep in status.dependencies if dep.name in {"gw1", "gw2"}
            }
            if gateway_deps == {"gw1": "HEALTHY", "gw2": "HEALTHY"}:
                break
            await asyncio.sleep(0.2)
        assert gateway_deps == {"gw1": "HEALTHY", "gw2": "HEALTHY"}
    finally:
        await harness.stop()
