"""UC-07 集成：真实 gRPC Server（make_server 全装配）成功路径、错误映射与取消。

不替换任何生产组件：Runtime/GatewayWriter/SnapshotManager 全部真实；
Start/Stop 依赖真实 assemble_gateway 的正式 UpdateVariables 目录预检。
"""

from __future__ import annotations

import asyncio
import contextlib

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from data_generator.infrastructure.persistence.snapshots import file_sha256

from ..conftest import ship_scenarios_document

pytestmark = [pytest.mark.integration, pytest.mark.safety, pytest.mark.timeout]


GATEWAY_DEVICES = {"GW01": [("pitch_angle", "float32", "feedback")]}


async def _start_full_stack(start_real_gateway, configs_root, dg_server):  # noqa: ANN001, ANN202
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GATEWAY_DEVICES)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600,
            mappings=[("gw-a", "GW01.pitch_angle")],
        ),
    )
    harness = await dg_server(service_path)
    return gateway, harness


async def test_all_fifteen_rpcs_callable(
    start_real_gateway,
    configs_root,
    dg_server,  # noqa: ANN001
) -> None:
    _gateway, harness = await _start_full_stack(start_real_gateway, configs_root, dg_server)
    info = await harness.common.GetServiceInfo(common.Empty())
    assert info.service_id == "data_generator"
    assert len(info.contract_version) >= 1
    status = await harness.common.GetServiceStatus(common.Empty())
    assert status.health in ("HEALTHY", "DEGRADED")
    capabilities = await harness.common.GetCapabilities(common.Empty())
    assert len(capabilities.rpc_names) == 15

    devices = await harness.dg.ListDevices(dg.ListDevicesRequest())
    assert {item.device_id for item in devices.devices} == {"GW01"}
    one = await harness.dg.GetDevice(common.ObjectId(id="GW01"))
    assert one.device.device_id == "GW01"
    scenarios = await harness.dg.ListScenarios(dg.ListScenariosRequest())
    assert scenarios.scenarios[0].scenario_id == "demo-random"

    snapshot = await harness.common.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    assert snapshot.revision
    assert snapshot.files
    metrics = await harness.common.GetMetrics(common.GetMetricsRequest())
    assert metrics.metrics is not None
    events_response = await harness.common.GetEvents(common.GetEventsRequest())
    assert events_response is not None
    diagnose = await harness.common.Diagnose(common.DiagnoseRequest(target_id="ghost"))
    assert diagnose.overall_status == "UNKNOWN"

    started = await harness.dg.StartGeneration(
        dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=snapshot.revision)
    )
    assert started.accepted, started.message
    stopped = await harness.dg.StopGeneration(
        dg.StopGenerationRequest(
            generation_id=started.generation_id, expected_revision=snapshot.revision
        )
    )
    assert stopped.accepted
    generation = await harness.dg.GetGeneration(common.ObjectId(id=started.generation_id))
    assert generation.generation.generation_id == started.generation_id


async def test_grpc_error_mapping(start_real_gateway, configs_root, dg_server) -> None:  # noqa: ANN001
    _gateway, harness = await _start_full_stack(start_real_gateway, configs_root, dg_server)
    with pytest.raises(grpc.RpcError) as device_404:
        await harness.dg.GetDevice(common.ObjectId(id="missing"))
    assert device_404.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.RpcError) as generation_404:
        await harness.dg.GetGeneration(common.ObjectId(id="g-x"))
    assert generation_404.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.RpcError) as bad_page:
        await harness.dg.ListDevices(dg.ListDevicesRequest(page_token="abc"))
    assert bad_page.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.RpcError) as bad_window:
        await harness.common.GetMetrics(common.GetMetricsRequest(scope="galaxy:x"))
    assert bad_window.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.RpcError) as huge_window:
        await harness.common.GetMetrics(common.GetMetricsRequest(window_ms=99_999_999))
    assert huge_window.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.RpcError) as bad_time:
        request = common.GetEventsRequest()
        request.to_time.GetCurrentTime()
        request.from_time.seconds = 2_000_000_000
        await harness.common.GetEvents(request)
    assert bad_time.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_start_revision_conflict_rejected_in_body(
    start_real_gateway,
    configs_root,
    dg_server,  # noqa: ANN001
) -> None:
    _gateway, harness = await _start_full_stack(start_real_gateway, configs_root, dg_server)
    response = await harness.dg.StartGeneration(
        dg.StartGenerationRequest(scenario_id="demo-random", expected_revision="stale")
    )
    assert response.accepted is False
    assert response.error_code == "REVISION_CONFLICT"


async def test_cancelled_push_releases_config_lock(
    start_real_gateway,
    configs_root,
    dg_server,  # noqa: ANN001
) -> None:
    _gateway, harness = await _start_full_stack(start_real_gateway, configs_root, dg_server)
    disk = await harness.common.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
    )
    files = [
        common.SnapshotFile(
            relative_path=item.relative_path,
            content=item.content,
            size_bytes=item.size_bytes,
            sha256=file_sha256(item.content),
        )
        for item in disk.files
    ]

    async def push_once() -> None:
        await harness.common.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(
                expected_revision=disk.revision,
                files=files,
            )
        )

    first = asyncio.ensure_future(push_once())
    await asyncio.sleep(0.1)  # 确保请求已到达服务端并持有配置锁
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError, grpc.RpcError):
        await first
    # shield 保证服务端事务继续完成；无论客户端如何取消，配置锁不得泄漏。
    second = await harness.common.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(expected_revision=disk.revision, files=files)
    )
    assert second.valid, second.validation
    # 服务仍可正常服务其他 RPC。
    info = await harness.common.GetServiceInfo(common.Empty())
    assert info.service_id == "data_generator"
