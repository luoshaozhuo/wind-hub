"""§14.4 表 14-2 查询系统测试（UR-09/UR-10，UC-03/UC-04）。

真实 gRPC/mTLS + 真实共享配置树；查询路径在设备端口不可达时仍成功，
证明只读查询不发起现场连接。
"""

from __future__ import annotations

import grpc
import pytest

from contracts import commander_pb2 as pb
from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.real_service]


@pytest.mark.normal
@pytest.mark.exception
async def test_devices_and_points(sys_pki, grpc_stack) -> None:
    """UC-03-S01：设备分页/详情/点表元数据真实返回；非法 ID 明确失败。"""
    _common, commander = grpc_stack.stubs(sys_pki, client="ops")
    page1 = await commander.ListDevices(pb.ListDevicesRequest(page_size=2), timeout=5)
    assert len(page1.devices) == 2 and page1.next_page_token
    first = page1.devices[0]
    assert first.device_id and len(first.revision) == 64

    detail = await commander.GetDevice(common_pb2.ObjectId(id=first.device_id), timeout=5)
    assert detail.device.device_id == first.device_id
    assert detail.device.protocol in {"modbus", "ads", "iec104"}
    assert len(detail.device.revision) == 64

    points = await commander.ListPoints(pb.ListPointsRequest(device_id=first.device_id), timeout=5)
    assert points.points
    sample = points.points[0]
    assert sample.point_id and sample.data_type
    assert isinstance(sample.writable, bool)
    writable = [p for p in points.points if p.writable]
    if writable:
        assert all(p.data_type for p in writable)

    # 异常侧：不存在的设备/点表查询明确 NOT_FOUND（不返回空壳伪装成功）。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await commander.GetDevice(common_pb2.ObjectId(id="NO-SUCH-DEVICE"), timeout=5)
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await commander.ListPoints(pb.ListPointsRequest(device_id="NO-SUCH-DEVICE"), timeout=5)
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND

    # 全部查询发生在 TZ 设备端口不可达的环境：查询路径不依赖现场连接。
    metrics = await _common.GetMetrics(
        common_pb2.GetMetricsRequest(scope=f"device:{first.device_id}", window_ms=0),
        timeout=5,
    )
    depth = next(m for m in metrics.metrics if m.name == "device_queue_depth")
    assert depth.value == 0.0
