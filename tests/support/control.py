"""System 层的 Collector 控制面 RPC helper。

ctl 已是只读诊断客户端（server-owned worker control）；system 测试需要
驱动 Task 生命周期时，直接以 gRPC 调用 CollectorControlService——与生产
中 Server → Collector 的控制路径一致。

placement 语义：StartTask/StartTaskInstance 要求先经 ApplyTaskPlacement
把 task 纳入当前 placement generation，否则 FAILED_PRECONDITION。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
from google.protobuf import empty_pb2

from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_core.rpc import collector_pb2 as pb
from wind_hub_core.rpc import collector_pb2_grpc as pb_grpc
from wind_hub_core.rpc.collector_codec import metrics_snapshot_to_dict

#: ``tests.support.process.start_collector`` 的默认 collector_id。
DEFAULT_WORKER_ID = "system-test"


async def apply_placement_and_start_instance(
    target: str,
    *,
    task_id: str,
    instance_id: str,
    worker_id: str = DEFAULT_WORKER_ID,
    generation: int = 1,
) -> None:
    """应用 placement 快照并启动单个 Task Instance。

    Args:
        target: Collector gRPC endpoint（``host:port``）。
        task_id: 纳入 placement 的 Task Definition ID。
        instance_id: 要启动的实例（``{task_id}:{device_id}``）。
        worker_id: placement 目标 worker（必须等于 Collector 的 collector_id）。
        generation: placement generation（StartTaskInstance 校验一致性）。
    """
    channel = grpc.aio.insecure_channel(target)
    stub = pb_grpc.CollectorControlServiceStub(channel)
    try:
        applied = await stub.ApplyTaskPlacement(
            pb.TaskPlacementSnapshotRequest(
                worker_id=worker_id, generation=generation, task_ids=[task_id]
            )
        )
        assert applied.success, "ApplyTaskPlacement rejected by collector"
        await stub.StartTaskInstance(
            pb.TaskInstanceStartRequest(
                instance_id=instance_id, placement_generation=generation
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

    config_hash 以 ``fingerprint_config_set`` 对当前磁盘配置集计算——与
    Collector prepare 路径的校验口径一致。用于验证「非法配置在 prepare
    阶段即被拒绝」的失败路径。
    """
    channel = grpc.aio.insecure_channel(target)
    stub = pb_grpc.CollectorControlServiceStub(channel)
    try:
        return await stub.PrepareConfig(
            pb.PrepareConfigRequest(
                revision_id=revision_id,
                config_hash=fingerprint_config_set(config_dir),
                force_reconfigure=force_reconfigure,
            )
        )
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
        return await stub.ActivateConfig(pb.ActivateConfigRequest(revision_id=revision_id))
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
        return metrics_snapshot_to_dict(response)
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
