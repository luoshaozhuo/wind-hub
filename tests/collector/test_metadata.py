"""GetPointMetadata v2 查询验收：真实装配 + 真实 grpc.aio 链路。

v2 元数据是只读的设备测点配置查询（无 v1 的 Task 覆盖列表语义——采集任务
每设备唯一，由设备型号点表解析）：

- 空 device_id / 未知设备 → INVALID_ARGUMENT / NOT_FOUND；
- 不给定 point_ids 时返回该设备点表全集；
- 未知测点缺行（不返回 found=False 占位），顺序按请求；
- data_type 有专属列，不混入 protocol_address；
- active_revision 与当前配置版本一致；超大批 → RESOURCE_EXHAUSTED。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
import pytest

from collector.assembly import assemble_collector
from collector.infrastructure.grpc.v2_server import CollectorRuntimeServiceV2
from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from tests.collector.config_v2.config_tree import ConfigTree, make_config_tree

pytestmark = pytest.mark.component


@pytest.fixture
async def metadata_stack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict]:
    tree: ConfigTree = make_config_tree(tmp_path)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ID", "metadata-collector")
    app = assemble_collector(
        tree.service,
        tree.instances,
        tree.templates,
        collector_id="metadata-collector",
    )
    server = grpc.aio.server()
    pb_grpc.add_CollectorRuntimeServiceServicer_to_server(CollectorRuntimeServiceV2(app), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield {"stub": pb_grpc.CollectorRuntimeServiceStub(channel), "app": app}
    finally:
        await channel.close()
        await server.stop(1)


async def test_empty_device_id_rejected(metadata_stack: dict) -> None:
    stub = metadata_stack["stub"]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetPointMetadata(pb.GetPointMetadataRequest(point_ids=["active_power"]))
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_unknown_device_not_found(metadata_stack: dict) -> None:
    stub = metadata_stack["stub"]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetPointMetadata(
            pb.GetPointMetadataRequest(device_id="ghost", point_ids=["active_power"])
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


async def test_default_returns_full_point_table_with_revision(metadata_stack: dict) -> None:
    stub = metadata_stack["stub"]
    app = metadata_stack["app"]
    response = await stub.GetPointMetadata(pb.GetPointMetadataRequest(device_id="WT01"))
    assert response.active_revision == app.config.active_revision
    assert response.active_revision
    ids = {row.point_id for row in response.points}
    assert ids == {"active_power"}


async def test_metadata_fields_authoritative(metadata_stack: dict) -> None:
    stub = metadata_stack["stub"]
    response = await stub.GetPointMetadata(
        pb.GetPointMetadataRequest(device_id="WT01", point_ids=["active_power"])
    )
    row = response.points[0]
    assert row.point_id == row.business_point_id == "active_power"
    assert row.data_type == "int32"
    assert row.unit_id == "kilowatt"
    assert row.quantity_id == "active_power"
    assert row.access  # 非空访问模式
    # data_type 不得混入协议地址字段。
    address = {field.key: field.value for field in row.protocol_address}
    assert "data_type" not in address
    assert address["register_type"].WhichOneof("kind") is not None


async def test_unknown_points_are_omitted_and_request_order_kept(metadata_stack: dict) -> None:
    stub = metadata_stack["stub"]
    response = await stub.GetPointMetadata(
        pb.GetPointMetadataRequest(
            device_id="WT01", point_ids=["missing-a", "active_power", "missing-b"]
        )
    )
    assert [row.point_id for row in response.points] == ["active_power"]


async def test_batch_over_limit_rejected(metadata_stack: dict) -> None:
    stub = metadata_stack["stub"]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetPointMetadata(
            pb.GetPointMetadataRequest(device_id="WT01", point_ids=[f"p{i}" for i in range(10001)])
        )
    assert exc.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
