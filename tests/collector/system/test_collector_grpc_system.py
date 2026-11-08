"""UC-07 system tests: the fixed 20-RPC contract over a real gRPC process.

Every RPC of CommonService (9) and CollectorService (11) is invoked against an
independent Collector process with real protocol slaves and a real NATS broker.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from typing import Any

import grpc
import pytest

from contracts import collector_pb2 as collector_pb
from contracts import common_pb2 as common
from tests.collector.system.conftest import ADMIN_METADATA, ClientsFactory
from tests.collector.system.helpers import (
    IEC104 as DEVICE_IEC104,
)
from tests.collector.system.helpers import (
    MODBUS as DEVICE_MODBUS,
)
from tests.collector.system.helpers import (
    disk_revision as _disk_revision,
)
from tests.collector.system.helpers import (
    start_task,
    stop_task,
)
from tests.collector.system.helpers import (
    wait_actual as _wait_actual,
)
from tests.collector.system.nats_verify import collect_realtime

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


async def test_uc07_s01_all_20_rpcs_real_invocation(collector, grpc_clients, real_nats) -> None:
    """Common 9 + Collector 11: each RPC returns a real, asserted result."""
    handle = await collector()
    channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    invoked: set[str] = set()

    # --- CommonService -----------------------------------------------------
    info = await config_stub.GetServiceInfo(common.Empty())
    invoked.add("GetServiceInfo")
    # 角色与实例身份分离：process_role 固定为 collector，service_id 是
    # 未显式指定时的主机名默认值（common.proto GetServiceInfoResponse）。
    assert info.process_role == "collector"
    assert info.service_id == socket.gethostname()
    assert info.contract_version == "windhub.common.v1"

    capabilities = await config_stub.GetCapabilities(common.Empty())
    invoked.add("GetCapabilities")
    assert len(capabilities.rpc_names) == 20
    assert set(capabilities.protocols) == {"ads", "modbus", "iec104"}

    revision0 = await _disk_revision(config_stub)
    invoked.add("GetConfigSnapshot")
    assert len(revision0) == 64
    paths = {
        item.relative_path
        for item in (
            await config_stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
                metadata=ADMIN_METADATA,
            )
        ).files
    }
    assert "services/collector.yaml" in paths
    assert "instances/devices.yaml" in paths

    pushed = await config_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[
                common.SnapshotFile(
                    relative_path=item.relative_path,
                    content=item.content,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                )
                for item in (
                    await config_stub.GetConfigSnapshot(
                        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
                        metadata=ADMIN_METADATA,
                    )
                ).files
            ],
            expected_revision=revision0,
        ),
        metadata=ADMIN_METADATA,
    )
    invoked.add("PushConfigSnapshot")
    assert pushed.valid
    candidate = await config_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(
            scope=common.SNAPSHOT_SCOPE_CANDIDATE,
            candidate_id=pushed.candidate_id,
        ),
        metadata=ADMIN_METADATA,
    )
    assert candidate.revision == revision0
    applied = await config_stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id,
            expected_revision=revision0,
        ),
        metadata=ADMIN_METADATA,
    )
    invoked.add("ApplyConfigSnapshot")
    assert applied.file_commit == revision0

    # --- CollectorService: inventory before acquisition -------------------
    devices = await device_stub.ListDevices(collector_pb.ListDevicesRequest())
    invoked.add("ListDevices")
    assert {item.device_id for item in devices.devices} == {DEVICE_MODBUS, DEVICE_IEC104}

    device = await device_stub.GetDevice(collector_pb.DeviceQuery(device_id=DEVICE_MODBUS))
    invoked.add("GetDevice")
    assert device.device.protocol == "modbus"
    assert device.device.endpoint.rsplit(":", 1)[-1].isdigit()

    points = await device_stub.ListPoints(collector_pb.ListPointsRequest(device_id=DEVICE_MODBUS))
    invoked.add("ListPoints")
    assert {item.point_id for item in points.points} == {
        "active_power",
        "rotor_speed",
        "gear_temp",
    }
    point = await device_stub.GetPoint(
        collector_pb.PointQuery(device_id=DEVICE_MODBUS, point_id="active_power")
    )
    invoked.add("GetPoint")
    assert point.point.data_type == "int32"
    assert point.point.readable

    tasks = await device_stub.ListTasks(collector_pb.ListTasksRequest())
    invoked.add("ListTasks")
    assert len(tasks.tasks) == 2

    task = await device_stub.GetTask(common.ObjectId(id=DEVICE_MODBUS))
    invoked.add("GetTask")
    assert task.task.desired_state == "STOP"

    # --- START through the durable task RPC -------------------------------
    await start_task(device_stub, config_stub, DEVICE_MODBUS)
    invoked.add("StartTask")
    assert await _wait_actual(device_stub, DEVICE_MODBUS, {"RUNNING"}) == "RUNNING"

    # Real Modbus values cross the wire through the active-read RPC.
    read = await device_stub.ReadPoints(
        collector_pb.ReadPointsRequest(device_id=DEVICE_MODBUS, timeout_ms=3000),
        metadata=ADMIN_METADATA,
    )
    invoked.add("ReadPoints")
    values = {sample.point_id: sample for sample in read.samples}
    assert set(values) == {"active_power", "rotor_speed", "gear_temp"}
    assert values["active_power"].quality == "good"
    # In-range integers share Core's scalar codec (sint64 arm); signedness is
    # carried by PointMetadata.data_type, not the oneof arm.
    assert values["active_power"].value.WhichOneof("kind") == "sint64_value"
    assert values["active_power"].value.sint64_value == 1500
    assert abs(values["rotor_speed"].value.double_value - 12.5) < 1e-6
    assert values["gear_temp"].value.sint64_value == 42

    # The same device's background poll really reached the independent NATS.
    observed = await collect_realtime(real_nats.url, count=3, timeout=10)
    assert all(item["device_id"] == DEVICE_MODBUS for item in observed)
    assert {item["point_id"] for item in observed} == {
        "active_power",
        "rotor_speed",
        "gear_temp",
    }

    publishing = await device_stub.GetPublishingStatus(collector_pb.GetPublishingStatusRequest())
    invoked.add("GetPublishingStatus")
    assert len(publishing.channels) == 1
    assert publishing.channels[0].nats_connected
    assert publishing.channels[0].published >= 3

    status = await config_stub.GetServiceStatus(common.Empty())
    invoked.add("GetServiceStatus")
    assert status.health == "HEALTHY"
    assert {dependency.name: dependency.health for dependency in status.dependencies} == {
        "acquisition": "HEALTHY",
        "nats": "HEALTHY",
    }

    # --- IEC104 listen session and real general interrogation --------------
    await start_task(device_stub, config_stub, DEVICE_IEC104)
    await asyncio.sleep(1.0)  # listener session establishment
    interrogation = await device_stub.InterrogateDevice(
        collector_pb.InterrogateDeviceRequest(device_id=DEVICE_IEC104, timeout_ms=5000),
        metadata=ADMIN_METADATA,
    )
    invoked.add("InterrogateDevice")
    assert interrogation.status == "SUCCESS"
    assert interrogation.error_code == ""
    assert {sample.point_id for sample in interrogation.samples} == {
        "meas_power",
        "meas_freq",
    }

    # --- Diagnose with evidence against the live Modbus chain -------------
    diagnose = await config_stub.Diagnose(
        common.DiagnoseRequest(target_id=DEVICE_MODBUS),
        metadata=ADMIN_METADATA,
    )
    invoked.add("Diagnose")
    assert diagnose.overall_status == "PASS"
    stages = {stage.stage: stage.status for stage in diagnose.stages}
    assert stages == {
        "NETWORK": "PASS",
        "PROTOCOL": "PASS",
        "ACQUISITION": "PASS",
        "NATS": "PASS",
        "BOUNDARY": "PASS",
    }

    # --- Metrics and Events -----------------------------------------------
    metrics = await config_stub.GetMetrics(common.GetMetricsRequest())
    invoked.add("GetMetrics")
    names = {metric.name for metric in metrics.metrics}
    # Realtime local submission is reported as client_submitted; PubAck is never
    # counted on the realtime channel (design §10.6 confirmation boundary).
    cumulative = {m.name: m.value for m in metrics.metrics}
    assert cumulative["collector.publisher.client_submitted"] >= 3
    assert cumulative["collector.publisher.pubacked"] == 0
    assert "collector.devices.running" in names
    windowed = await config_stub.GetMetrics(common.GetMetricsRequest(window_ms=60000))
    window_names = {metric.name for metric in windowed.metrics}
    assert "collector.points.collected" in window_names
    device_metrics = await config_stub.GetMetrics(common.GetMetricsRequest(scope="device:modbus-1"))
    assert all("modbus-1" in metric.name for metric in device_metrics.metrics)

    events = await config_stub.GetEvents(
        common.GetEventsRequest(page_size=10), metadata=ADMIN_METADATA
    )
    invoked.add("GetEvents")
    assert events.events

    # --- STOP leaves the device at STOP for clean process shutdown --------
    await stop_task(device_stub, config_stub, DEVICE_MODBUS)
    invoked.add("StopTask")
    assert await _wait_actual(device_stub, DEVICE_MODBUS, {"STOPPED"}) == "STOPPED"
    await stop_task(device_stub, config_stub, DEVICE_IEC104)
    assert await _wait_actual(device_stub, DEVICE_IEC104, {"STOPPED"}) == "STOPPED"

    expected = {
        "GetServiceInfo",
        "GetServiceStatus",
        "GetCapabilities",
        "GetConfigSnapshot",
        "PushConfigSnapshot",
        "ApplyConfigSnapshot",
        "GetMetrics",
        "GetEvents",
        "Diagnose",
        "ListDevices",
        "GetDevice",
        "ListPoints",
        "GetPoint",
        "ReadPoints",
        "ListTasks",
        "GetTask",
        "StartTask",
        "StopTask",
        "GetPublishingStatus",
        "InterrogateDevice",
    }
    assert invoked == expected, f"missing RPC invocations: {sorted(expected - invoked)}"


async def test_uc07_s02_authorization_pagination_and_invalid_arguments(
    collector, grpc_clients
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)

    # Admin surface without a token is refused, not merely empty.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
        )
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.StartTask(common.OperationRequest(id=DEVICE_MODBUS))
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetEvents(common.GetEventsRequest())
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.ReadPoints(
            collector_pb.ReadPointsRequest(device_id=DEVICE_MODBUS, point_ids=["active_power"])
        )
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.Diagnose(common.DiagnoseRequest(target_id=DEVICE_MODBUS))
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    bad_token = (("x-windhub-admin-token", "wrong"),)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
            metadata=bad_token,
        )
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    # Unknown objects map to NOT_FOUND.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.GetDevice(collector_pb.DeviceQuery(device_id="ghost"))
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.GetPoint(
            collector_pb.PointQuery(device_id=DEVICE_MODBUS, point_id="ghost")
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND

    # Real pagination tokens across the two devices.
    first = await device_stub.ListDevices(collector_pb.ListDevicesRequest(page_size=1))
    assert len(first.devices) == 1
    assert first.next_page_token
    second = await device_stub.ListDevices(
        collector_pb.ListDevicesRequest(page_size=1, page_token=first.next_page_token)
    )
    assert {page.devices[0].device_id for page in (first, second)} == {
        DEVICE_MODBUS,
        DEVICE_IEC104,
    }
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.ListDevices(
            collector_pb.ListDevicesRequest(page_size=1, page_token="not-a-number")
        )
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    # Invalid snapshot scope / metrics scope / window.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(), metadata=ADMIN_METADATA
        )
    assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetMetrics(common.GetMetricsRequest(scope="galaxy"))
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetMetrics(common.GetMetricsRequest(window_ms=1500))
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    # Malformed candidate is rejected as a validation result, not committed.
    pushed = await config_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[
                common.SnapshotFile(
                    relative_path="services/collector.yaml",
                    content=b"schema_version: 1\n",
                    size_bytes=18,
                )
            ],
            expected_revision=await _disk_revision(config_stub),
        ),
        metadata=ADMIN_METADATA,
    )
    assert not pushed.valid
    assert pushed.validation

    # Interrogation is IEC104-only.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.InterrogateDevice(
            collector_pb.InterrogateDeviceRequest(device_id=DEVICE_MODBUS),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_uc07_s02_event_paging_evicts_oldest_session_at_capacity(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
) -> None:
    """The 16-session/300 s event paging cache evicts the oldest session first."""
    from tests.collector.system.blackhole import TcpBlackhole

    hole = TcpBlackhole()
    await hole.start()
    try:
        # Real watchdog/poll cycles against a blackholed endpoint produce
        # genuine telemetry events without injecting anything in-process; wait
        # until at least two real events exist so a page token is issued.
        handle = await collector(blackhole_port=hole.port, watchdog_interval=0.2, task_interval=0.2)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, "modbus-hold")
        deadline = asyncio.get_running_loop().time() + 15
        count = 0
        while asyncio.get_running_loop().time() < deadline:
            seen = await config_stub.GetEvents(
                common.GetEventsRequest(page_size=500), metadata=ADMIN_METADATA
            )
            count = len(seen.events)
            if count >= 2:
                break
            await asyncio.sleep(0.5)
        assert count >= 2, f"expected real failure events, got {count}"

        first = await config_stub.GetEvents(
            common.GetEventsRequest(page_size=1), metadata=ADMIN_METADATA
        )
        assert first.events
        assert first.next_page_token
        tokens = [first.next_page_token]
        # Open 16 further live paging sessions; capacity is 16, FIFO eviction.
        for _ in range(16):
            page = await config_stub.GetEvents(
                common.GetEventsRequest(page_size=1), metadata=ADMIN_METADATA
            )
            assert page.next_page_token
            tokens.append(page.next_page_token)

        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await config_stub.GetEvents(
                common.GetEventsRequest(page_size=1, page_token=tokens[0]),
                metadata=ADMIN_METADATA,
            )
        assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert "event page expired" in exc.value.details()

        # The newest session still pages through real stored events.
        latest = await config_stub.GetEvents(
            common.GetEventsRequest(page_size=1, page_token=tokens[-1]),
            metadata=ADMIN_METADATA,
        )
        assert latest.events
    finally:
        await hole.stop()


async def test_uc07_s03_deadline_unconfirmed_and_shutdown_unavailable(
    collector, grpc_clients
) -> None:
    from tests.collector.system.blackhole import TcpBlackhole

    hole = TcpBlackhole()
    await hole.start()
    try:
        handle = await collector(blackhole_port=hole.port)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)

        revision = await _disk_revision(config_stub)
        started = await device_stub.StartTask(
            common.OperationRequest(id="modbus-hold", expected_revision=revision),
            metadata=ADMIN_METADATA,
        )
        assert started.accepted

        # A read that cannot settle by the RPC deadline is reported UNCONFIRMED,
        # never as a definite failure; the underlying I/O stays owned.
        response = await device_stub.ReadPoints(
            collector_pb.ReadPointsRequest(
                device_id="modbus-hold",
                point_ids=["active_power"],
                timeout_ms=300,
            ),
            metadata=ADMIN_METADATA,
        )
        assert len(response.samples) == 1
        assert response.samples[0].error_code == "TIMEOUT_UNCONFIRMED"
        assert response.samples[0].quality == "bad"

        # A stale revision cannot linearize a second control operation.
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await device_stub.StartTask(
                common.OperationRequest(id=DEVICE_MODBUS, expected_revision="0" * 64),
                metadata=ADMIN_METADATA,
            )
        assert exc.value.code() == grpc.StatusCode.ABORTED
    finally:
        await hole.stop()
