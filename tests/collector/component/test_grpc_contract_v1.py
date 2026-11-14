"""Collector v1 complete gRPC contract component tests (STOP devices only)."""

from __future__ import annotations

import shutil
from pathlib import Path

import grpc
import pytest
import yaml

from collector.assembly import assemble_collector
from contracts import collector_pb2 as collector
from contracts import collector_pb2_grpc as collector_rpc
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc

pytestmark = [pytest.mark.component, pytest.mark.contract]
CONFIG_ROOT = Path(__file__).resolve().parents[3] / "configs"
AUTH = (("x-windhub-admin-token", "test-collector-admin"),)


@pytest.mark.asyncio
async def test_all_services_and_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", AUTH[0][1])
    for folder in ("instances", "templates"):
        shutil.copytree(CONFIG_ROOT / folder, tmp_path / folder)
    service = yaml.safe_load(
        (CONFIG_ROOT / "services" / "collector.yaml").read_text(encoding="utf-8")
    )
    service["file_version"] = "1.0.0"
    service_path = tmp_path / "services" / "collector.yaml"
    service_path.parent.mkdir()
    service_path.write_text(yaml.safe_dump(service), encoding="utf-8")
    app = await assemble_collector(
        service_path,
        state_directory=tmp_path / "state",
        grpc_port=0,
        collector_id="collector-test",
    )
    await app.start()
    try:
        async with grpc.aio.insecure_channel(app.endpoint) as channel:
            shared = common_rpc.CommonServiceStub(channel)
            devices = collector_rpc.CollectorServiceStub(channel)

            capabilities = await shared.GetCapabilities(common.Empty())
            assert len(capabilities.rpc_names) == 20
            assert {"ads", "modbus", "iec104"} == set(capabilities.protocols)
            status = await shared.GetServiceStatus(common.Empty())
            assert status.health in {"HEALTHY", "DEGRADED"}
            assert status.dependencies
            samples = await shared.GetMetrics(common.GetMetricsRequest())
            assert any(item.name == "collector.devices.total" for item in samples.metrics)
            window = await shared.GetMetrics(common.GetMetricsRequest(window_ms=60000))
            assert all(item.window_ms == 60000 for item in window.metrics)
            events = await shared.GetEvents(common.GetEventsRequest(), metadata=AUTH)
            assert events.next_page_token == ""
            diagnostic = await shared.Diagnose(common.DiagnoseRequest(), metadata=AUTH)
            assert diagnostic.stages
            assert diagnostic.overall_status in {"PASS", "UNKNOWN", "FAIL"}

            found = await devices.ListDevices(collector.ListDevicesRequest())
            # The shared demo fleet is multi-device; the contract under test is
            # inventory self-consistency, not a fleet size of one: every enabled
            # device owns exactly one task.
            assert found.devices
            tasks = await devices.ListTasks(collector.ListTasksRequest())
            assert len(tasks.tasks) == len(found.devices)
            device_id = found.devices[0].device_id
            detail = await devices.GetDevice(collector.DeviceQuery(device_id=device_id))
            assert detail.device.device_id == device_id
            points = await devices.ListPoints(collector.ListPointsRequest(device_id=device_id))
            assert points.points
            point = await devices.GetPoint(
                collector.PointQuery(device_id=device_id, point_id=points.points[0].point_id)
            )
            assert point.point.point_id == points.points[0].point_id
            assert point.point.data_type
            task = await devices.GetTask(common.ObjectId(id=device_id))
            assert task.task.desired_state == "STOP"
            # Task.revision is the writable DISK revision clients echo back in
            # expected_revision; it must match ListTasks and the DISK snapshot.
            disk = await shared.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
                metadata=AUTH,
            )
            assert task.task.revision == disk.revision
            assert tasks.tasks[0].revision == disk.revision
            publishing = await devices.GetPublishingStatus(collector.GetPublishingStatusRequest())
            assert publishing.channels and publishing.channels[0].channel == "realtime"
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await devices.StartTask(
                    common.OperationRequest(id=device_id, expected_revision="bad")
                )
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await devices.ReadPoints(collector.ReadPointsRequest(device_id=device_id))
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await shared.GetMetrics(common.GetMetricsRequest(window_ms=12345))
            assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await app.stop()


def test_configuration_redaction_handles_nested_credentials() -> None:
    from collector.infrastructure.adapters.inbound.grpc.config_service import _redact_yaml

    raw = (
        b"messaging:\n"
        b"  password: supersecret\n"
        b"  options:\n"
        b"    api_token: hidden123\n"
        b"    host: example.org\n"
    )
    sanitized = _redact_yaml(raw)
    assert b"supersecret" not in sanitized
    assert b"hidden123" not in sanitized
    assert b"REDACTED" in sanitized
    assert b"example.org" in sanitized


def test_non_secret_snapshot_is_byte_identical() -> None:
    from collector.infrastructure.adapters.inbound.grpc.config_service import _redact_yaml

    raw = b"# retain original formatting\nvalue: [1, 2, 3]\n"
    assert _redact_yaml(raw) == raw


def test_empty_secret_placeholder_is_byte_identical() -> None:
    # A template that only declares an empty password placeholder must survive
    # a DISK read/rewrite unchanged, while the secret-bearing case is still
    # scrubbed (see test_configuration_redaction_handles_nested_credentials).
    from collector.infrastructure.adapters.inbound.grpc.config_service import _redact_yaml

    raw = (
        b"# leading comment must survive\n"
        b"protocol_options:\n"
        b'  username: "Administrator"\n'
        b'  password: ""\n'
    )
    assert _redact_yaml(raw) == raw
