"""Collector gRPC 出站适配器。

全部调用使用 collector.proto 生成的 Runtime/Control Stub；本模块只负责把
Protobuf 转换为 Server 应用层既有 Python DTO/dict 边界。
"""

from __future__ import annotations

from typing import Any, cast

import grpc
from google.protobuf import empty_pb2

from wind_hub_core.rpc import collector_pb2 as pb
from wind_hub_core.rpc import collector_pb2_grpc as pb_grpc
from wind_hub_core.rpc.collector_codec import (
    collector_info_to_dict,
    device_info_to_dict,
    metrics_snapshot_to_dict,
    runtime_status_to_dict,
    sink_info_to_dict,
    task_instance_to_dict,
    task_summary_to_dict,
)
from wind_hub_server.adapter.outbound.grpc.common import GrpcClientBase
from wind_hub_server.application.port.worker import CollectorPlacementRejectedError


def _placement_error(exc: grpc.aio.AioRpcError) -> Exception:
    """把 placement 相关的 gRPC 拒绝映射为端口层语义错误，其余原样抛出。

    Collector 对 placement 栅栏违例返回 FAILED_PRECONDITION（未下发快照、
    generation 不一致）或 PERMISSION_DENIED（任务不在快照内）——这两类
    对 Server 意味着「placement 不再安全」，必须由应用层重建栅栏；
    其他错误码（UNAVAILABLE 等）按普通 RPC 故障向上传播。
    """
    if exc.code() in (
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.PERMISSION_DENIED,
    ):
        return CollectorPlacementRejectedError(exc.details() or exc.code().name)
    # grpcio 无类型 stub，AioRpcError 注解解析为 Any；
    # 运行时 AioRpcError 是 Exception 子类，这里显式收窄。
    return cast(Exception, exc)


class CollectorGrpcClient(GrpcClientBase):
    """通过 generated Collector Stub 查询和控制独立 Collector。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._runtime_stub = pb_grpc.CollectorRuntimeServiceStub(self._channel)
        self._control_stub = pb_grpc.CollectorControlServiceStub(self._channel)

    async def config_status(self) -> dict[str, Any]:
        """返回 Collector 配置与进程身份状态。"""
        response = await self._runtime_stub.GetCollectorInfo(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return collector_info_to_dict(response)

    async def runtime_status(self) -> dict[str, Any]:
        """返回 Collector Runtime 聚合状态。"""
        response = await self._runtime_stub.GetRuntimeStatus(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return runtime_status_to_dict(response)

    async def metrics_snapshot(self) -> dict[str, Any]:
        """返回 Collector 本地采集指标快照。"""
        response = await self._runtime_stub.GetMetricsSnapshot(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return metrics_snapshot_to_dict(response)

    async def list_devices(self) -> list[dict[str, Any]]:
        """列出 Collector 当前设备状态。"""
        response = await self._runtime_stub.ListDevices(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return [device_info_to_dict(item) for item in response.items]

    async def list_sinks(self) -> list[dict[str, Any]]:
        """列出 Collector 当前 Sink 状态。"""
        response = await self._runtime_stub.ListSinks(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return [sink_info_to_dict(item) for item in response.items]

    async def verify_sink(self, name: str) -> dict[str, Any]:
        """检查指定 Sink 健康状态。"""
        response = await self._runtime_stub.VerifySink(
            pb.SinkRequest(name=name),
            timeout=self.default_timeout,
        )
        return {
            "success": response.success,
            "message": response.message or None,
            "queue_depth": response.queue_depth,
        }

    async def write_test_sink(self, name: str) -> dict[str, Any]:
        """执行指定 Sink 的诊断写入。"""
        response = await self._runtime_stub.WriteTestSink(
            pb.SinkRequest(name=name),
            timeout=self.default_timeout,
        )
        return {
            "success": response.success,
            "message": response.message or None,
            "queue_depth": response.queue_depth,
        }

    async def list_tasks(self) -> list[dict[str, Any]]:
        """列出 Task Definition 聚合状态。"""
        response = await self._runtime_stub.ListTasks(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return [task_summary_to_dict(item) for item in response.items]

    async def list_task_instances(self) -> list[dict[str, Any]]:
        """列出全部 Task Instance。"""
        response = await self._runtime_stub.ListTaskInstances(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return [task_instance_to_dict(item) for item in response.items]

    async def apply_task_placement(
        self,
        worker_id: str,
        generation: int,
        task_ids: list[str],
    ) -> dict[str, Any]:
        """向 Collector 下发当前 placement 快照（Start 的前置栅栏）。

        Raises:
            CollectorPlacementRejectedError: Collector 拒绝快照
                （worker_id 不匹配或栅栏内部状态错误）。
        """
        try:
            response = await self._control_stub.ApplyTaskPlacement(
                pb.TaskPlacementSnapshotRequest(
                    worker_id=worker_id,
                    generation=generation,
                    task_ids=task_ids,
                ),
                timeout=self.default_timeout,
            )
        except grpc.aio.AioRpcError as exc:
            raise _placement_error(exc) from exc
        return {
            "success": response.success,
            "generation": response.generation,
            "task_count": response.task_count,
        }

    async def start_task(self, task_id: str, placement_generation: int) -> dict[str, Any]:
        """启动指定 Task 的全部实例（受 placement 栅栏保护）。

        Raises:
            CollectorPlacementRejectedError: 未先下发 placement 快照或
                generation 与 Collector 当前栅栏不一致。
        """
        try:
            response = await self._control_stub.StartTask(
                pb.TaskStartRequest(
                    task_id=task_id,
                    placement_generation=placement_generation,
                ),
                timeout=self.default_timeout,
            )
        except grpc.aio.AioRpcError as exc:
            raise _placement_error(exc) from exc
        return task_summary_to_dict(response)

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """停止指定 Task 的全部实例。"""
        response = await self._control_stub.StopTask(
            pb.TaskIdRequest(task_id=task_id),
            timeout=self.default_timeout,
        )
        return task_summary_to_dict(response)

    async def start_task_instance(
        self,
        instance_id: str,
        placement_generation: int,
    ) -> dict[str, Any]:
        """启动单个 Task Instance（受 placement 栅栏保护）。

        Raises:
            CollectorPlacementRejectedError: 未先下发 placement 快照或
                generation 与 Collector 当前栅栏不一致。
        """
        try:
            response = await self._control_stub.StartTaskInstance(
                pb.TaskInstanceStartRequest(
                    instance_id=instance_id,
                    placement_generation=placement_generation,
                ),
                timeout=self.default_timeout,
            )
        except grpc.aio.AioRpcError as exc:
            raise _placement_error(exc) from exc
        return task_instance_to_dict(response)

    async def stop_task_instance(self, instance_id: str) -> dict[str, Any]:
        """停止单个 Task Instance。"""
        response = await self._control_stub.StopTaskInstance(
            pb.InstanceIdRequest(instance_id=instance_id),
            timeout=self.default_timeout,
        )
        return task_instance_to_dict(response)

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
        *,
        force_reconfigure: bool = False,
    ) -> dict[str, Any]:
        """准备 Collector 指定配置 revision。"""
        response = await self._control_stub.PrepareConfig(
            pb.PrepareConfigRequest(
                revision_id=revision_id,
                config_hash=config_hash,
                force_reconfigure=force_reconfigure,
            ),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "config_hash": response.config_hash,
            "errors": list(response.errors),
            "duration_ms": response.duration_ms,
        }

    async def activate_config(self, revision_id: str) -> dict[str, Any]:
        """激活 Collector 指定 prepared revision。"""
        response = await self._control_stub.ActivateConfig(
            pb.ActivateConfigRequest(revision_id=revision_id),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "active_config_hash": response.active_config_hash,
            "errors": list(response.errors),
            "duration_ms": response.duration_ms,
        }

    async def abort_config(self, revision_id: str) -> dict[str, Any]:
        """撤销 Collector 指定 prepared revision。"""
        response = await self._control_stub.AbortConfig(
            pb.AbortConfigRequest(revision_id=revision_id),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "aborted": response.aborted,
        }
