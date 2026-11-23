"""Data Generator system configuration: Repository ownership, restart and crash recovery."""

from __future__ import annotations

import json

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import dg_stubs, ship_scenarios_document

pytestmark = [pytest.mark.system, pytest.mark.reliability]

ONE_FLOAT = {"GW01": [("pitch_angle", "float32", "feedback")]}
DISK = common.SNAPSHOT_SCOPE_DISK
RUNTIME = common.SNAPSHOT_SCOPE_RUNTIME
CANDIDATE = common.SNAPSHOT_SCOPE_CANDIDATE


async def _revision(stub) -> str:  # noqa: ANN001
    response = await stub.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=RUNTIME))
    return str(response.revision)


async def _denied_push(common_stub, revision: str, files) -> None:  # noqa: ANN001
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await common_stub.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(expected_revision=revision, files=files)
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


@pytest.mark.reload
async def test_get_push_apply(start_real_gateway, configs_root, dg_subprocess) -> None:  # noqa: ANN001
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
        runtime = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=RUNTIME)
        )
        assert disk.revision == revision
        assert {item.relative_path for item in disk.files} == {
            item.relative_path for item in runtime.files
        }
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message

        # A local writer cannot introduce or activate another Scenario version.
        await _denied_push(common_stub, revision, list(disk.files))
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await common_stub.ApplyConfigSnapshot(
                common.ApplyConfigSnapshotRequest(
                    candidate_id="not-published", expected_revision=revision
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert await _revision(common_stub) == revision
        generation = await dg_stub.GetGeneration(common.ObjectId(id=started.generation_id))
        assert generation.generation.actual_state in {"STARTING", "RUNNING"}
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=revision
            )
        )
        assert stopped.accepted
    finally:
        await channel.close()


@pytest.mark.conflict
async def test_invalid_candidate_and_conflict(
    start_real_gateway, configs_root, dg_subprocess
) -> None:  # noqa: ANN001
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
        assert started.accepted
        await _denied_push(common_stub, "stale-revision", list(disk.files))
        await _denied_push(common_stub, revision, list(disk.files))
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await common_stub.ApplyConfigSnapshot(
                common.ApplyConfigSnapshotRequest(
                    candidate_id="does-not-exist", expected_revision=revision
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await common_stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=CANDIDATE, candidate_id="does-not-exist")
            )
        assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert exc.value.details() == "candidate not found"
        assert await _revision(common_stub) == revision
        state = await dg_stub.GetGeneration(common.ObjectId(id=started.generation_id))
        assert state.generation.actual_state in {"STARTING", "RUNNING"}
    finally:
        await channel.close()


@pytest.mark.restart
async def test_restart_required(start_real_gateway, configs_root, dg_subprocess) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    bind, process = await dg_subprocess(service_path)
    channel, _dg, common_stub = await dg_stubs(bind)
    revision = await _revision(common_stub)
    try:
        root = service_path.parent.parent
        target = service_path.with_name("service_data-generator_v1.0.1.yaml")
        document = yaml.safe_load(service_path.read_text(encoding="utf-8"))
        document["file_version"] = "1.0.1"
        document["output"]["grpc_timeout_seconds"] = 1.5
        target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        manifest = root / "deployments/deployment_dg-test_v1.0.0.yaml"
        selection = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        selection["services"]["data-generator"]["file_version"] = "1.0.1"
        manifest.write_text(yaml.safe_dump(selection), encoding="utf-8")
        # Running Main does not silently adopt the newly selected Service.
        assert await _revision(common_stub) == revision
    finally:
        await channel.close()
    process.terminate()
    process.wait(timeout=15)
    bind_next, _next = await dg_subprocess(target)
    next_channel, _next_dg, next_common = await dg_stubs(bind_next)
    try:
        updated = await next_common.GetConfigSnapshot(common.GetConfigSnapshotRequest(scope=DISK))
        assert updated.revision != revision
        assert any(
            item.relative_path.endswith("service_data-generator_v1.0.1.yaml")
            for item in updated.files
        )
    finally:
        await next_channel.close()


@pytest.mark.fault_injection
async def test_apply_failure_recovery(start_real_gateway, configs_root, dg_subprocess) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    root = service_path.parent.parent
    relative = service_path.relative_to(root).as_posix()
    original = service_path.read_bytes()
    tampered = original + b"# interrupted private write\n"
    journal = root / ".dg_transaction"
    backup = journal / "backup" / relative
    staged = journal / "new" / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    staged.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(original)
    staged.write_bytes(tampered)
    (journal / "active.json").write_text(
        json.dumps({"existing": [relative], "changed": [relative]}),
        encoding="utf-8",
    )
    service_path.write_bytes(tampered)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        assert not journal.exists()
        assert service_path.read_bytes() == original
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted
    finally:
        await channel.close()
