"""UC-04 System：独立进程的配置 Get/Push/Apply、冲突、重启区分与事务恢复。

覆盖 §14.2 标识：
test_get_push_apply / test_invalid_candidate_and_conflict /
test_restart_required / test_apply_failure_recovery。
"""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import (
    dg_stubs,
    read_gateway_variable,
    replace_snapshot_files,
    ship_scenarios_document,
)

pytestmark = [pytest.mark.system, pytest.mark.reliability]

ONE_FLOAT = {"GW01": [("pitch_angle", "float32", "feedback")]}
DISK = common.SNAPSHOT_SCOPE_DISK
RUNTIME = common.SNAPSHOT_SCOPE_RUNTIME
CANDIDATE = common.SNAPSHOT_SCOPE_CANDIDATE


def _by_suffix(snapshot: common.GetConfigSnapshotResponse, suffix: str) -> str:
    return next(
        item.relative_path for item in snapshot.files if item.relative_path.endswith(suffix)
    )


async def _revision(stub) -> str:  # noqa: ANN001
    snapshot = await stub.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=RUNTIME))
    return str(snapshot.revision)


async def _push(common_stub, revision: str, files: list[common.SnapshotFile]):  # noqa: ANN001
    return await common_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(expected_revision=revision, files=files)
    )


@pytest.mark.reload
async def test_get_push_apply(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-04-S01：三作用域可读；Push 不生效；Apply 热生效并报告活动实例旧版本绑定。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        disk = await common_stub.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=DISK))
        assert disk.revision == revision and disk.files
        runtime = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=RUNTIME)
        )
        assert {item.relative_path for item in disk.files} == {
            item.relative_path for item in runtime.files
        }

        # 活动 Generation：Apply 后该场景应报告 BOUND_OLD_VERSION。
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message

        scenario_rel = _by_suffix(disk, "instances/data-generator.yaml")
        new_doc = yaml.safe_load(
            next(item.content for item in disk.files if item.relative_path == scenario_rel)
        )
        new_doc["scenarios"][0]["version"] = "1.0.1"
        new_doc["scenarios"][0]["source"]["interval_seconds"] = 0.2
        files = replace_snapshot_files(
            disk, {scenario_rel: yaml.safe_dump(new_doc, sort_keys=False).encode()}
        )
        pushed = await _push(common_stub, revision, files)
        assert pushed.valid, pushed.validation
        candidate = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=CANDIDATE, candidate_id=pushed.candidate_id)
        )
        assert candidate.revision != revision
        # Push 不生效：运行版本与 ListScenarios 仍为旧版。
        scenarios = await dg_stub.ListScenarios(dg.ListScenariosRequest())
        assert scenarios.scenarios[0].active_version == "1.0.0"

        applied = await common_stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id=pushed.candidate_id, expected_revision=revision
            )
        )
        assert applied.file_commit == "COMMITTED"
        assert applied.restart_required is False
        (result,) = applied.apply_results
        assert result.object_id == "demo-random"
        assert result.status == "BOUND_OLD_VERSION"
        assert await _revision(common_stub) == applied.effective_revision

        # 旧 Generation 停止后，新启动按 1.0.1 / 0.2s 生效并真实写出。
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=applied.effective_revision
            )
        )
        assert stopped.accepted
        scenarios = await dg_stub.ListScenarios(dg.ListScenariosRequest())
        assert scenarios.scenarios[0].active_version == "1.0.1"
        restarted = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(
                scenario_id="demo-random",
                scenario_version="1.0.1",
                expected_revision=applied.effective_revision,
            )
        )
        assert restarted.accepted, restarted.message
        for _ in range(50):
            sample = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.pitch_angle")
            if sample.quality == "GOOD":
                break
            await asyncio.sleep(0.1)
        assert sample.quality == "GOOD"
    finally:
        await channel.close()


@pytest.mark.conflict
async def test_invalid_candidate_and_conflict(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-04-S02：陈旧修订号、Core 只读文件篡改、未知候选均被拒；旧配置持续有效。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=0.3,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        disk = await common_stub.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=DISK))
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message

        # 1) 陈旧 expected_revision。
        stale = await _push(common_stub, "stale-revision", list(disk.files))
        assert stale.valid is False
        assert stale.validation[0].code == "REVISION_CONFLICT"

        # 2) 篡改 Core 共享只读文件（site.yaml）→ OWNERSHIP_DENIED。
        site_rel = _by_suffix(disk, "instances/site.yaml")
        site_item = next(item for item in disk.files if item.relative_path == site_rel)
        mutated = bytes(site_item.content).replace(b"schema_version: 1", b"schema_version: 1 ", 1)
        if mutated == site_item.content:
            mutated = site_item.content + b"\n# acceptance mutation\n"
        files = replace_snapshot_files(disk, {site_rel: mutated})
        denied = await _push(common_stub, revision, files)
        assert denied.valid is False
        assert denied.validation[0].code == "OWNERSHIP_DENIED", denied.validation

        # 3) Apply 未知候选 → 文件提交失败，运行配置与活动任务不变。
        applied = await common_stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id="does-not-exist", expected_revision=revision
            )
        )
        assert applied.file_commit == "FAILED"
        assert applied.apply_results[0].error_code in {
            "CONFIG_VALIDATION_FAILED",
            "FILE_COMMIT_FAILED",
        }
        assert await _revision(common_stub) == revision
        state = await dg_stub.GetGeneration(common.ObjectId(id=started.generation_id))
        assert state.generation.actual_state == "RUNNING"
    finally:
        await channel.close()


@pytest.mark.restart
async def test_restart_required(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-04-S03：服务文件变更提交但不热生效，重启进程后按新修订运行。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    bind, process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        disk = await common_stub.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=DISK))
        service_rel = _by_suffix(disk, "services/data-generator.yaml")
        service_doc = yaml.safe_load(
            next(item.content for item in disk.files if item.relative_path == service_rel)
        )
        assert service_doc["output"]["grpc_timeout_seconds"] != 1.5
        service_doc["output"]["grpc_timeout_seconds"] = 1.5
        files = replace_snapshot_files(
            disk, {service_rel: yaml.safe_dump(service_doc, sort_keys=False).encode()}
        )
        pushed = await _push(common_stub, revision, files)
        assert pushed.valid, pushed.validation
        applied = await common_stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id=pushed.candidate_id, expected_revision=revision
            )
        )
        assert applied.file_commit == "COMMITTED"
        assert applied.restart_required is True
        # 验收实测 P2：restart-required 提交时响应 effective_revision 仍为旧 runtime
        # 修订（server 成功分支取 runtime.settings，热重启场景未刷新）；磁盘修订才是
        # 提交后的权威修订，重启后进程按磁盘修订运行。
        assert applied.effective_revision == revision
        disk_after = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=DISK)
        )
        committed_revision = disk_after.revision
        assert committed_revision != revision
        # P2-4：ApplicationResult.message 必须同时给出磁盘修订与当前生效修订。
        messages = [result.message for result in applied.apply_results if result.message]
        assert messages, applied
        assert any(f"disk_revision={committed_revision}" in text for text in messages), messages
        assert any(f"effective_revision={revision}" in text for text in messages), messages
    finally:
        await channel.close()

    # 重启同一配置：进程退出后重新拉起，读到已提交修订，事务目录无残留。
    process.terminate()
    process.wait(timeout=15)
    bind_2, _process_2 = await dg_subprocess(service_path)
    channel_2, _dg_2, common_2 = await dg_stubs(bind_2)
    try:
        assert await _revision(common_2) == committed_revision
        assert not (service_path.parent.parent / ".dg_transaction").exists()
    finally:
        await channel_2.close()


@pytest.mark.fault_injection
async def test_apply_failure_recovery(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-04-S03：提交中断留下活动事务日志，重启时回滚，旧配置完好可用。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    root = service_path.parent.parent
    scenario_rel = "instances/data-generator.yaml"
    original = (root / scenario_rel).read_bytes()
    tampered = original.replace(b"version: 1.0.0", b"version: 9.9.9", 1)
    assert tampered != original
    journal = root / ".dg_transaction"
    (journal / "backup" / "instances").mkdir(parents=True)
    (journal / "new" / "instances").mkdir(parents=True)
    (journal / "backup" / scenario_rel).write_bytes(original)
    (journal / "new" / scenario_rel).write_bytes(tampered)
    (journal / "active.json").write_text(
        json.dumps({"existing": [scenario_rel], "changed": [scenario_rel]}),
        encoding="utf-8",
    )

    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        # 恢复后旧配置生效：版本仍为 1.0.0，可正常启动；事务目录已清理。
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        disk = await common_stub.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=DISK))
        scenario_bytes = next(
            item.content for item in disk.files if item.relative_path.endswith(scenario_rel)
        )
        assert b"9.9.9" not in scenario_bytes
        assert not journal.exists()
    finally:
        await channel.close()
