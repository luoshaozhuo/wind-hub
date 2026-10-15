"""新 Collector gRPC 控制面组件测试。

真实装配（assemble_collector）+ 真实 gRPC Server/Channel，设备指向
127.0.0.1 已关闭端口（connect 立即拒绝），验证 Runtime/Control 两个
Servicer 的 wire contract 与应用语义。
"""

from __future__ import annotations

import grpc
import pytest
from google.protobuf import empty_pb2

from collector.assembly import CollectorApp, assemble_collector
from collector.infrastructure.grpc import build_grpc_server
from core.infrastructure.rpc import collector_pb2 as pb
from core.infrastructure.rpc import collector_pb2_grpc as pb_grpc
from tests.support.new_collector import write_collector_config_tree


@pytest.fixture
async def running(tmp_path):
    """装配并启动 Collector + gRPC 控制面；yield (app, runtime_stub, control_stub)。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "127.0.0.1", "port": 1},  # 立即拒绝
            }
        },
        runtime={"connect_timeout": 0.5},
    )
    app = assemble_collector(config_dir, collector_id="collector-test")
    server = build_grpc_server(app, app.identity, host="127.0.0.1", port=0)
    await app.start()
    await server.start()
    channel = grpc.aio.insecure_channel(server.endpoint)
    try:
        yield (
            app,
            pb_grpc.CollectorRuntimeServiceStub(channel),
            pb_grpc.CollectorControlServiceStub(channel),
        )
    finally:
        await channel.close()
        await server.stop(0)
        await app.stop()


async def test_get_collector_info(running):
    app: CollectorApp
    app, runtime_stub, _ = running
    info = await runtime_stub.GetCollectorInfo(empty_pb2.Empty())
    assert info.component == "wind-hub-collector"
    assert info.collector_id == "collector-test"
    assert info.boot_id == app.identity.boot_id
    assert info.config_hash == app.config_hash
    assert info.boot_config_hash == app.config_hash
    assert info.active_revision == "startup"
    assert info.runtime_running is True


async def test_runtime_status_and_devices(running):
    _, runtime_stub, _ = running
    status = await runtime_stub.GetRuntimeStatus(empty_pb2.Empty())
    assert status.running is True
    assert status.device_count == 1
    assert status.sink_count == 1
    assert status.devices_connected == 0  # 127.0.0.1:1 连接被拒绝

    devices = await runtime_stub.ListDevices(empty_pb2.Empty())
    assert len(devices.items) == 1
    assert devices.items[0].device_id == "dev1"
    assert devices.items[0].protocol == "modbus"
    assert devices.items[0].connected is False


async def test_metrics_snapshot(running):
    _, runtime_stub, _ = running
    snapshot = await runtime_stub.GetMetricsSnapshot(empty_pb2.Empty())
    # 设备连接失败至少被记录一次（启动时的 connect_all）
    assert snapshot.counters.connect_failures >= 1
    assert snapshot.device_connect_failures[0].key == "dev1"


async def test_task_lifecycle(running):
    _, runtime_stub, control_stub = running

    tasks = await runtime_stub.ListTasks(empty_pb2.Empty())
    assert len(tasks.items) == 1
    assert tasks.items[0].task_id == "t1"
    assert tasks.items[0].runtime_state == "stopped"

    # 未授权启动必须拒绝；随后由控制面下发当前任务归属。
    with pytest.raises(grpc.aio.AioRpcError) as denied:
        await control_stub.StartTask(pb.TaskStartRequest(task_id="t1"))
    assert denied.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    info = await runtime_stub.GetCollectorInfo(empty_pb2.Empty())
    placement = await control_stub.ApplyTaskPlacement(
        pb.TaskPlacementSnapshotRequest(
            worker_id=info.collector_id,
            generation=1,
            task_ids=["t1"],
        )
    )
    assert placement.success
    assert placement.task_count == 1
    summary = await control_stub.StartTask(
        pb.TaskStartRequest(task_id="t1", placement_generation=1)
    )
    assert summary.runtime_state == "running"
    assert summary.running_instances == 1

    instances = await runtime_stub.ListTaskInstances(empty_pb2.Empty())
    assert len(instances.items) == 1
    instance_id = instances.items[0].instance_id
    assert instances.items[0].state == "running"

    detail = await control_stub.StopTaskInstance(pb.InstanceIdRequest(instance_id=instance_id))
    assert detail.state == "stopped"

    summary = await control_stub.StopTask(pb.TaskIdRequest(task_id="t1"))
    assert summary.runtime_state == "stopped"

    # 未知 task / instance 映射为 NOT_FOUND
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await runtime_stub.GetTask(pb.TaskIdRequest(task_id="ghost"))
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await runtime_stub.GetTaskInstance(pb.InstanceIdRequest(instance_id="ghost"))
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


async def test_sink_check_and_write_test(running, tmp_path):
    _, runtime_stub, _ = running

    sinks = await runtime_stub.ListSinks(empty_pb2.Empty())
    assert len(sinks.items) == 1
    assert sinks.items[0].name == "s1"
    assert sinks.items[0].healthy is True

    verify = await runtime_stub.VerifySink(pb.SinkRequest(name="s1"))
    assert verify.success is True

    result = await runtime_stub.WriteTestSink(pb.SinkRequest(name="s1"))
    assert result.success is True
    out = tmp_path / "out.csv"
    assert out.exists()
    assert "_write_test" in out.read_text()

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await runtime_stub.VerifySink(pb.SinkRequest(name="ghost"))
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


async def test_config_transaction(running):
    app: CollectorApp
    app, runtime_stub, control_stub = running

    # 配置未变化：prepare 成功但 diff 为空
    prepared = await control_stub.PrepareConfig(
        pb.PrepareConfigRequest(revision_id="r2", config_hash=app.config_hash)
    )
    assert prepared.success is True
    assert prepared.config_hash == app.config_hash
    assert prepared.diff.points_changed is False
    assert not prepared.diff.devices.added

    # 指纹不匹配：prepare 失败但 RPC 本身成功返回 errors
    mismatch = await control_stub.PrepareConfig(
        pb.PrepareConfigRequest(revision_id="r3", config_hash="0" * 64)
    )
    assert mismatch.success is False
    assert mismatch.errors

    activated = await control_stub.ActivateConfig(pb.ActivateConfigRequest(revision_id="r2"))
    assert activated.success is True
    assert activated.active_config_hash == app.config_hash

    info = await runtime_stub.GetCollectorInfo(empty_pb2.Empty())
    assert info.active_revision == "r2"

    # activate 后无 prepared；abort r2 幂等返回 aborted=False
    aborted = await control_stub.AbortConfig(pb.AbortConfigRequest(revision_id="r2"))
    assert aborted.success is True
    assert aborted.aborted is False


async def test_get_points_metadata_wire_contract(running):
    """真实 gRPC 通道验证 GetPointsMetadata；设备未连接也必须可查询。"""
    app: CollectorApp
    app, runtime_stub, _ = running

    # 设备连接被拒绝（127.0.0.1:1），元数据查询仍基于纯配置成功。
    devices = await runtime_stub.ListDevices(empty_pb2.Empty())
    assert devices.items[0].connected is False

    response = await runtime_stub.GetPointsMetadata(
        pb.PointsMetadataRequest(device_id="dev1", point_ids=["p1", "ghost", "p1"])
    )
    assert response.device_id == "dev1"
    assert response.active_revision == app.config.active_revision
    assert response.active_config_hash == app.config.config_hash
    assert [row.point_id for row in response.points] == ["p1", "ghost", "p1"]

    found = response.points[0]
    assert found.found is True
    assert found.metadata.point_id == "p1"
    assert found.metadata.data_type == "float32"
    assert found.metadata.access == "read_write"
    assert list(found.metadata.task_ids) == ["t1"]
    assert list(found.metadata.sink_ids) == ["s1"]
    address = {field.key: field.value for field in found.metadata.configured_address}
    assert address["register_type"].string_value == "holding"
    assert "data_type" not in address

    assert response.points[1].found is False
    assert response.points[1].error == "point not found"
    assert not response.points[1].HasField("metadata")
    assert response.points[2] == found


async def test_get_points_metadata_error_mapping(running):
    _, runtime_stub, _ = running

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await runtime_stub.GetPointsMetadata(
            pb.PointsMetadataRequest(device_id="ghost", point_ids=["p1"])
        )
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND

    for request in (
        pb.PointsMetadataRequest(device_id="", point_ids=["p1"]),
        pb.PointsMetadataRequest(device_id="dev1", point_ids=[]),
        pb.PointsMetadataRequest(device_id="dev1", point_ids=["p1", ""]),
    ):
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await runtime_stub.GetPointsMetadata(request)
        assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT
