"""Shared gRPC helpers for Collector v1 system tests."""

from __future__ import annotations

import asyncio

from contracts import collector_pb2 as collector
from contracts import common_pb2 as common
from contracts.collector_pb2_grpc import CollectorServiceStub
from contracts.common_pb2_grpc import CommonServiceStub

from .conftest import ADMIN_METADATA

MODBUS = "modbus-1"
IEC104 = "iec104-1"


async def disk_revision(config_stub: CommonServiceStub) -> str:
    response = await config_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
        metadata=ADMIN_METADATA,
    )
    return str(response.revision)


async def snapshot_files(config_stub: CommonServiceStub) -> dict[str, bytes]:
    response = await config_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
        metadata=ADMIN_METADATA,
    )
    return {item.relative_path: item.content for item in response.files}


async def push_apply(
    config_stub: CommonServiceStub, files: dict[str, bytes], revision: str
) -> common.ApplyConfigSnapshotResponse:
    pushed = await config_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[
                common.SnapshotFile(
                    relative_path=path,
                    content=content,
                    size_bytes=len(content),
                )
                for path, content in sorted(files.items())
            ],
            expected_revision=revision,
        ),
        metadata=ADMIN_METADATA,
    )
    assert pushed.valid, pushed.validation
    result: common.ApplyConfigSnapshotResponse = await config_stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id,
            expected_revision=revision,
        ),
        metadata=ADMIN_METADATA,
    )
    return result


async def start_task(
    device_stub: CollectorServiceStub, config_stub: CommonServiceStub, device_id: str
) -> None:
    revision = await disk_revision(config_stub)
    await device_stub.StartTask(
        common.OperationRequest(id=device_id, expected_revision=revision),
        metadata=ADMIN_METADATA,
    )


async def stop_task(
    device_stub: CollectorServiceStub, config_stub: CommonServiceStub, device_id: str
) -> None:
    revision = await disk_revision(config_stub)
    await device_stub.StopTask(
        common.OperationRequest(id=device_id, expected_revision=revision),
        metadata=ADMIN_METADATA,
    )


async def wait_actual(
    device_stub: CollectorServiceStub,
    device_id: str,
    expected: set[str],
    *,
    timeout: float = 15.0,
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        task = await device_stub.GetTask(common.ObjectId(id=device_id))
        last = str(task.task.actual_state)
        if last in expected:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"{device_id} never reached {expected}; last={last}")


async def publishing(device_stub: CollectorServiceStub) -> collector.PublishingStatus:
    response = await device_stub.GetPublishingStatus(collector.GetPublishingStatusRequest())
    assert len(response.channels) == 1
    channel: collector.PublishingStatus = response.channels[0]
    return channel
