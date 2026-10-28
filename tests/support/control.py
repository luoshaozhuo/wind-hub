"""System / Reliability 层的 Collector 控制面 RPC helper。

system 测试需要驱动 Task 生命周期时，直接以 gRPC 调用
CollectorControlService——与 CLI → Collector 的生产控制
路径一致；不经过任何 Server 中转。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
from google.protobuf import empty_pb2

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as pb_grpc
from core.infrastructure.config import config_dir_digest


def _metrics_snapshot_to_dict(response: pb.MetricsSnapshotResponse) -> dict[str, Any]:
    """把 MetricsSnapshotResponse 转换为便于断言的 dict。"""
    return {
        "counters": {
            field.name: getattr(response.counters, field.name)
            for field in response.counters.DESCRIPTOR.fields
        },
        "device_connect_failures": {
            item.key: item.value for item in response.device_connect_failures
        },
        "device_reconnects": {item.key: item.value for item in response.device_reconnects},
        "events": [
            {
                "timestamp": item.timestamp.ToJsonString(),
                "kind": item.kind,
                "object": item.object,
                "message": item.message,
            }
            for item in response.events
        ],
    }


async def start_task_instance(
    target: str,
    *,
    task_id: str,
    instance_id: str,
    placement_generation: int = 1,
) -> None:
    """启动单个已配置 Task Instance。

    生产路径下 Start 受 placement 栅栏保护：先向 Collector 下发当前
    worker 的任务归属快照，再带 generation 启动。worker_id 不经参数
    传递，而是向目标 Collector 查询其身份，避免测试与 collector_id
    传参重复维护。

    Args:
        target: Collector gRPC endpoint（``host:port``）。
        task_id: Task Definition ID（placement 快照中的任务归属）。
        instance_id: 要启动的实例（``{task_id}:{device_id}``）。
        placement_generation: placement 快照代次，默认 1。
    """
    channel = grpc.aio.insecure_channel(target)
    control = pb_grpc.CollectorControlServiceStub(channel)
    runtime = pb_grpc.CollectorRuntimeServiceStub(channel)
    try:
        info = await runtime.GetCollectorInfo(empty_pb2.Empty())
        placement = await control.ApplyTaskPlacement(
            pb.TaskPlacementSnapshotRequest(
                worker_id=info.collector_id,
                generation=placement_generation,
                task_ids=[task_id],
            )
        )
        assert placement.success, f"ApplyTaskPlacement rejected: {list(placement.errors)}"
        await control.StartTaskInstance(
            pb.TaskInstanceStartRequest(
                instance_id=instance_id,
                placement_generation=placement_generation,
            )
        )
    finally:
        await channel.close()


async def prepare_config(
    target: str,
    *,
    config_dir: Path,
    revision_id: str,
    force_reconfigure: bool = False,
) -> pb.PrepareConfigResponse:
    """调用 PrepareConfig 并返回原始响应（success/errors 由调用方断言）。

    config_hash 以 ``config_dir_digest`` 对当前磁盘配置集计算——与
    Collector prepare 路径的校验口径一致。用于验证「非法配置在 prepare
    阶段即被拒绝」的失败路径。
    """
    channel = grpc.aio.insecure_channel(target)
    stub = pb_grpc.CollectorControlServiceStub(channel)
    try:
        response: pb.PrepareConfigResponse = await stub.PrepareConfig(
            pb.PrepareConfigRequest(
                revision_id=revision_id,
                config_hash=config_dir_digest(config_dir),
                force_reconfigure=force_reconfigure,
            )
        )
        return response
    finally:
        await channel.close()


async def reload_config(
    target: str,
    *,
    config_dir: Path,
    revision_id: str,
    force_reconfigure: bool = False,
) -> pb.ActivateConfigResponse:
    """经 Prepare/Activate 两段事务热重载 Collector 配置（等价生产 reload）。

    断言 Prepare 成功（Prepare 失败属于调用方配置错误，应用 prepare_config
    显式验证）；Activate 的 success/diff/errors 由调用方断言。

    Returns:
        ActivateConfigResponse。
    """
    prepared = await prepare_config(
        target,
        config_dir=config_dir,
        revision_id=revision_id,
        force_reconfigure=force_reconfigure,
    )
    assert prepared.success, f"PrepareConfig failed: {list(prepared.errors)}"
    channel = grpc.aio.insecure_channel(target)
    stub = pb_grpc.CollectorControlServiceStub(channel)
    try:
        response: pb.ActivateConfigResponse = await stub.ActivateConfig(
            pb.ActivateConfigRequest(revision_id=revision_id)
        )
        return response
    finally:
        await channel.close()


async def metrics_snapshot(target: str) -> dict[str, Any]:
    """查询 Collector 指标快照（connect_failures/reconnects 等累计计数）。

    backoff 与 reconnect 行为的边界观测点：计数单调不减，测试用前后差值
    度量窗口内的 connect 尝试次数。
    """
    channel = grpc.aio.insecure_channel(target)
    stub = pb_grpc.CollectorRuntimeServiceStub(channel)
    try:
        response = await stub.GetMetricsSnapshot(empty_pb2.Empty(), timeout=5.0)
        return _metrics_snapshot_to_dict(response)
    finally:
        await channel.close()


async def stop_instance(target: str, instance_id: str) -> None:
    """停止单个 Task Instance（幂等）。"""
    channel = grpc.aio.insecure_channel(target)
    stub = pb_grpc.CollectorControlServiceStub(channel)
    try:
        await stub.StopTaskInstance(pb.InstanceIdRequest(instance_id=instance_id))
    finally:
        await channel.close()
