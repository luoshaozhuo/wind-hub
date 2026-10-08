"""新 Commander gRPC Server 组件测试。

真实装配（assemble_commander）+ 真实 gRPC Server/Channel；设备 lazy
连接，离线设备的读写映射为稳定 gRPC status。
"""

from __future__ import annotations

import grpc
import pytest
from google.protobuf import empty_pb2

from commander.assembly import CommanderApp, assemble_commander
from commander.infrastructure.grpc import build_grpc_server
from commander.infrastructure.grpc import commander_pb2 as pb
from commander.infrastructure.grpc import commander_pb2_grpc as pb_grpc
from tests.support.new_commander import write_minimal_config_tree


@pytest.fixture
async def running(tmp_path):
    """装配并启动 Commander + gRPC Server；yield (app, stub)。"""
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "127.0.0.1", "port": 1},  # 立即拒绝
            }
        ],
        runtime={"connect_timeout": 0.5, "read_timeout": 0.5},
    )
    app = assemble_commander(config_dir)
    server = build_grpc_server(app, host="127.0.0.1", port=0)
    await app.start()
    await server.start()
    channel = grpc.aio.insecure_channel(server.endpoint)
    try:
        yield app, pb_grpc.CommanderServiceStub(channel)
    finally:
        await channel.close()
        await server.stop(0)
        await app.stop()


async def test_get_status_and_list_devices(running):
    app: CommanderApp
    app, stub = running
    status = await stub.GetStatus(empty_pb2.Empty())
    assert status.running is True
    assert status.device_count == 1
    assert status.healthy_devices == 0  # lazy 连接，未操作时不连通
    assert status.active_config_hash == app.config_hash

    devices = await stub.ListDevices(empty_pb2.Empty())
    assert len(devices.devices) == 1
    device = devices.devices[0]
    assert device.device_id == "dev1"
    assert device.protocol == "modbus"
    assert device.host == "127.0.0.1"
    assert device.port == 1
    assert device.enabled is True


async def test_config_transaction(running):
    app: CommanderApp
    app, stub = running

    prepared = await stub.PrepareConfig(
        pb.PrepareConfigRequest(revision_id="r2", config_hash=app.config_hash)
    )
    assert prepared.success is True
    assert prepared.config_hash == app.config_hash

    # 指纹不匹配映射为 INVALID_ARGUMENT 或 INTERNAL（ValueError → INVALID_ARGUMENT）
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await stub.PrepareConfig(pb.PrepareConfigRequest(revision_id="r3", config_hash="0" * 64))
    assert excinfo.value.code() in (
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.INTERNAL,
    )

    activated = await stub.ActivateConfig(pb.ActivateConfigRequest(revision_id="r2"))
    assert activated.success is True
    assert activated.active_config_hash == app.config_hash

    aborted = await stub.AbortConfig(pb.AbortConfigRequest(revision_id="r2"))
    assert aborted.success is True
    assert aborted.aborted is False


async def test_read_point_offline_device_maps_status(running):
    _, stub = running
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await stub.ReadPoint(pb.ReadPointRequest(device_id="dev1", point_id="p1"))
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await stub.ReadPoint(pb.ReadPointRequest(device_id="ghost", point_id="p1"))
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


async def test_write_point_offline_device_returns_failure_result(running):
    _, stub = running
    request = pb.WritePointRequest(
        device_id="dev1",
        point_id="p1",
        value=pb.ScalarValue(double_value=1.0),
    )
    result = await stub.WritePoint(request)
    # 命令分发到离线设备：CommandResult 级失败而非 RPC 错误
    assert result.command_id
    assert result.success is False
    assert result.error


async def test_verify_device_offline(running):
    _, stub = running
    response = await stub.VerifyDevice(pb.VerifyDeviceRequest(device_id="dev1", timeout=0.3))
    assert response.device_id == "dev1"
    assert response.ok is False
    assert response.stages
