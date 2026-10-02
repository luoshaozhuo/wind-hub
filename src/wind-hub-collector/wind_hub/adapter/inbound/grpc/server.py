"""Collector gRPC 控制面。

本适配器实现 collector.proto 生成的 Runtime/Control Servicer。正常采集数据
不经过该服务；这里只承载低频状态查询、Task 控制、Sink 检查与配置事务。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import grpc
from google.protobuf import empty_pb2, wrappers_pb2

from wind_hub.application.runtime.collector_identity import CollectorIdentity
from wind_hub.assembly import AssembledRuntime
from wind_hub_core.model.point import PointValue
from wind_hub_core.rpc import collector_pb2 as pb
from wind_hub_core.rpc import collector_pb2_grpc as pb_grpc


def _required(value: str, field: str) -> str:
    """校验 protobuf 必填字符串。"""
    if not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


async def _abort_invalid(context: grpc.aio.ServicerContext, message: str) -> None:
    """终止非法参数请求。"""
    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, message)


def _task_summary_to_proto(item) -> pb.TaskSummaryMessage:
    """把 TaskSummary 转换为 wire message。"""
    message = pb.TaskSummaryMessage(
        task_id=item.task_id,
        point_group=item.point_group,
        targets=list(item.targets),
        enabled=item.enabled,
        runtime_state=item.runtime_state,
        instance_count=item.instance_count,
        running_instances=item.running_instances,
        stopped_instances=item.stopped_instances,
        failed_instances=item.failed_instances,
    )
    if item.device is not None:
        message.device.CopyFrom(wrappers_pb2.StringValue(value=item.device))
    if item.device_group is not None:
        message.device_group.CopyFrom(wrappers_pb2.StringValue(value=item.device_group))
    if item.interval is not None:
        message.interval.CopyFrom(wrappers_pb2.DoubleValue(value=item.interval))
    return message


def _task_instance_to_proto(item) -> pb.TaskInstanceMessage:
    """把 TaskInstanceDetail 转换为 wire message。"""
    message = pb.TaskInstanceMessage(
        instance_id=item.instance_id,
        task_id=item.task_id,
        device_id=item.device_id,
        point_group=item.point_group,
        targets=list(item.targets),
        state=item.state.value,
    )
    if item.interval is not None:
        message.interval.CopyFrom(wrappers_pb2.DoubleValue(value=item.interval))
    return message


def _diff_to_proto(diff) -> pb.ConfigDiffMessage:
    """把结构化 ConfigDiff 转换为 wire message。"""
    return pb.ConfigDiffMessage(
        devices=pb.DeviceDiffMessage(
            added=list(diff.devices.added),
            removed=list(diff.devices.removed),
            updated=list(diff.devices.updated),
            unchanged=list(diff.devices.unchanged),
        ),
        sinks=pb.SinkDiffMessage(
            added=list(diff.sinks.added),
            removed=list(diff.sinks.removed),
            updated=list(diff.sinks.updated),
            unchanged=list(diff.sinks.unchanged),
        ),
        tasks=pb.TaskDiffMessage(
            added=list(diff.tasks.added),
            removed=list(diff.tasks.removed),
            updated=list(diff.tasks.updated),
            unchanged=list(diff.tasks.unchanged),
        ),
        points_changed=diff.points_changed,
        point_tables_changed=list(diff.point_tables_changed),
        units_changed=diff.units_changed,
    )


@dataclass(slots=True)
class CollectorGrpcServer:
    """Collector gRPC Server 生命周期包装。"""

    server: grpc.aio.Server
    endpoint: str

    async def start(self) -> None:
        """开始监听 gRPC endpoint。"""
        await self.server.start()

    async def stop(self, grace: float = 5.0) -> None:
        """停止接收新请求并等待正在执行的 RPC。"""
        await self.server.stop(grace)


class CollectorRuntimeService(pb_grpc.CollectorRuntimeServiceServicer):
    """Collector 运行态只读查询。"""

    def __init__(
        self,
        runtime: AssembledRuntime,
        identity: CollectorIdentity,
    ) -> None:
        self._runtime = runtime
        self._identity = identity

    async def GetCollectorInfo(self, request, context) -> pb.CollectorInfoResponse:
        """返回 Collector 身份、配置指纹与 Runtime 运行事实。"""
        del request, context
        return pb.CollectorInfoResponse(
            component="wind-hub-collector",
            collector_id=self._identity.collector_id,
            boot_id=self._identity.boot_id,
            config_hash=self._runtime.config.config_hash,
            active_config_hash=self._runtime.config.config_hash,
            prepared_config_hash=self._runtime.config.prepared_hash or "",
            boot_config_hash=self._identity.config_hash,
            config_revision=self._identity.config_revision or "",
            active_revision=self._runtime.config.active_revision,
            prepared_revision=self._runtime.config.prepared_revision or "",
            runtime_running=self._runtime.runtime.running,
        )

    async def GetRuntimeStatus(self, request, context) -> pb.RuntimeStatusResponse:
        """返回 Runtime 聚合状态快照。"""
        del request, context
        status = await self._runtime.query.status()
        response = pb.RuntimeStatusResponse(
            running=status.running,
            device_count=status.device_count,
            sink_count=status.sink_count,
            devices_connected=status.devices_connected,
            sinks_healthy=status.sinks_healthy,
            points_collected=status.points_collected,
            points_routed=status.points_routed,
            points_dropped=status.points_dropped,
        )
        for item in status.acquisitions:
            row = pb.AcquisitionInfoMessage(
                instance_id=item.instance_id,
                task_id=item.task_id,
                device_id=item.device_id,
                point_group=item.point_group,
                running=item.running,
                consecutive_failures=item.consecutive_failures,
                last_error=item.last_error or "",
            )
            if item.last_duration is not None:
                row.last_duration.CopyFrom(
                    wrappers_pb2.DoubleValue(value=item.last_duration)
                )
            response.acquisitions.append(row)
        return response

    async def GetMetricsSnapshot(self, request, context) -> pb.MetricsSnapshotResponse:
        """返回 Collector 本地采集质量累计计数与近期事件。"""
        del request, context
        snapshot = self._runtime.metrics_state.snapshot()
        counters = snapshot["counters"]
        response = pb.MetricsSnapshotResponse(
            counters=pb.MetricsCounters(
                points_total=int(counters["points_total"]),
                points_bad=int(counters["points_bad"]),
                acquisition_runs=int(counters["acquisition_runs"]),
                acquisition_failures=int(counters["acquisition_failures"]),
                acquisition_partial=int(counters["acquisition_partial"]),
                missed_cycles=int(counters["missed_cycles"]),
                poll_overruns=int(counters["poll_overruns"]),
                connect_failures=int(counters["connect_failures"]),
                reconnects=int(counters["reconnects"]),
            )
        )
        response.device_connect_failures.extend(
            pb.StringCounter(key=key, value=int(value))
            for key, value in sorted(snapshot["device_connect_failures"].items())
        )
        response.device_reconnects.extend(
            pb.StringCounter(key=key, value=int(value))
            for key, value in sorted(snapshot["device_reconnects"].items())
        )
        for event in snapshot["events"]:
            row = pb.MetricsEvent(
                kind=str(event["kind"]),
                object=str(event["object"]),
                message=str(event["message"]),
            )
            row.timestamp.FromDatetime(datetime.fromisoformat(str(event["timestamp"])))
            response.events.append(row)
        return response

    async def ListTasks(self, request, context) -> pb.ListTasksResponse:
        """列出 Task Definition 与聚合运行状态。"""
        del request, context
        items = await self._runtime.tasks.list_task_summaries()
        return pb.ListTasksResponse(
            items=[_task_summary_to_proto(item) for item in items]
        )

    async def GetTask(
        self,
        request: pb.TaskIdRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskSummaryMessage:
        """按稳定 task_id 查询 Task 聚合状态。"""
        try:
            item = await self._runtime.tasks.get_task_summary(
                _required(request.task_id, "task_id")
            )
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task: {exc.args[0]}")
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _task_summary_to_proto(item)

    async def ListTaskInstances(
        self,
        request,
        context,
    ) -> pb.ListTaskInstancesResponse:
        """列出当前展开的全部 Task Instance。"""
        del request, context
        items = await self._runtime.tasks.list_instances()
        return pb.ListTaskInstancesResponse(
            items=[_task_instance_to_proto(item) for item in items]
        )

    async def GetTaskInstance(
        self,
        request: pb.InstanceIdRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskInstanceMessage:
        """按 instance_id 查询单个 Task Instance。"""
        try:
            item = await self._runtime.tasks.get_instance(
                _required(request.instance_id, "instance_id")
            )
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"unknown task instance: {exc.args[0]}",
            )
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _task_instance_to_proto(item)

    async def ListDevices(self, request, context) -> pb.ListDevicesResponse:
        """列出当前 Runtime 注册设备及连接状态。"""
        del request, context
        devices = await self._runtime.query.list_devices()
        response = pb.ListDevicesResponse()
        for item in devices:
            row = pb.DeviceInfoMessage(
                device_id=item.device_id,
                protocol=item.protocol,
                connected=item.connected,
                consecutive_failures=item.consecutive_failures,
                last_error=item.last_error or "",
            )
            if item.last_seen is not None:
                row.last_seen.FromDatetime(item.last_seen)
            response.items.append(row)
        return response

    async def ListSinks(self, request, context) -> pb.ListSinksResponse:
        """列出当前 Runtime Sink 健康状态与队列深度。"""
        del request, context
        health = self._runtime.runtime.health()
        depths = self._runtime.runtime.sink_queue_depths()
        response = pb.ListSinksResponse()
        for name in self._runtime.runtime.sinks:
            current = health.get(name)
            response.items.append(
                pb.SinkInfoMessage(
                    name=name,
                    healthy=bool(current.healthy) if current is not None else False,
                    message=(current.message or "") if current is not None else "",
                    queue_depth=int(depths.get(name, 0)),
                )
            )
        return response

    async def VerifySink(
        self,
        request: pb.SinkRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.SinkOperationResponse:
        """返回当前运行 Sink 的真实 health 与队列深度。"""
        try:
            name = _required(request.name, "name")
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        sink = self._runtime.runtime.sinks.get(name)
        if sink is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown sink: {name}")
            raise AssertionError("context.abort must terminate the RPC")
        health = sink.health()
        return pb.SinkOperationResponse(
            success=bool(health.healthy),
            message=health.message or "",
            queue_depth=int(self._runtime.runtime.sink_queue_depths().get(name, 0)),
        )

    async def WriteTestSink(
        self,
        request: pb.SinkRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.SinkOperationResponse:
        """向当前运行 Sink 写入一条明确标记的诊断 PointValue。"""
        try:
            name = _required(request.name, "name")
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        sink = self._runtime.runtime.sinks.get(name)
        if sink is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown sink: {name}")
            raise AssertionError("context.abort must terminate the RPC")
        try:
            await sink.write(
                [PointValue(device_id="_diagnostic", point_id="_write_test", value=1)]
            )
            await sink.flush()
        except Exception as exc:
            return pb.SinkOperationResponse(
                success=False,
                message=str(exc) or type(exc).__name__,
            )
        return pb.SinkOperationResponse(success=True)


class CollectorControlService(pb_grpc.CollectorControlServiceServicer):
    """Collector 低频运行控制。"""

    def __init__(
        self,
        runtime: AssembledRuntime,
        identity: CollectorIdentity,
    ) -> None:
        self._runtime = runtime
        self._identity = identity
        self._placement_generation = 0
        self._assigned_task_ids: frozenset[str] = frozenset()

    async def ApplyTaskPlacement(
        self,
        request: pb.TaskPlacementSnapshotRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskPlacementSnapshotResponse:
        """应用 Server 下发的完整 Task placement 快照。"""
        worker_id = _required(request.worker_id, "worker_id")
        if worker_id != self._identity.collector_id:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "placement worker_id does not match collector_id",
            )
            raise AssertionError("context.abort must terminate the RPC")
        generation = int(request.generation)
        if generation <= 0:
            await _abort_invalid(context, "generation must be greater than 0")
            raise AssertionError("context.abort must terminate the RPC")

        task_ids = frozenset(str(task_id) for task_id in request.task_ids)
        if any(not task_id for task_id in task_ids):
            await _abort_invalid(context, "task_ids must not contain empty values")
            raise AssertionError("context.abort must terminate the RPC")
        known_task_ids = set(self._runtime.runtime.task_definitions())
        unknown_task_ids = sorted(task_ids - known_task_ids)
        if unknown_task_ids:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "placement contains unknown task(s): " + ", ".join(unknown_task_ids),
            )
            raise AssertionError("context.abort must terminate the RPC")

        if generation < self._placement_generation:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "placement generation is stale",
            )
            raise AssertionError("context.abort must terminate the RPC")
        if (
            generation == self._placement_generation
            and task_ids != self._assigned_task_ids
        ):
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "same placement generation has different task set",
            )
            raise AssertionError("context.abort must terminate the RPC")

        self._placement_generation = generation
        self._assigned_task_ids = task_ids
        return pb.TaskPlacementSnapshotResponse(
            success=True,
            generation=generation,
            task_count=len(task_ids),
        )

    async def _require_start_authority(
        self,
        task_id: str,
        placement_generation: int,
        context: grpc.aio.ServicerContext,
    ) -> None:
        """校验 Start 请求与当前 placement 快照一致。"""
        if self._placement_generation <= 0:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "task placement has not been applied",
            )
            raise AssertionError("context.abort must terminate the RPC")
        if placement_generation != self._placement_generation:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "task placement generation mismatch",
            )
            raise AssertionError("context.abort must terminate the RPC")
        if task_id not in self._assigned_task_ids:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"task '{task_id}' is not assigned to this collector",
            )
            raise AssertionError("context.abort must terminate the RPC")

    async def StartTask(
        self,
        request: pb.TaskStartRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskSummaryMessage:
        """启动一个 Task 当前展开的全部实例。"""
        return await self._set_task(request, context, start=True)

    async def StopTask(
        self,
        request: pb.TaskIdRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskSummaryMessage:
        """停止一个 Task 当前展开的全部实例。"""
        return await self._set_task(request, context, start=False)

    async def _set_task(
        self,
        request: pb.TaskIdRequest,
        context: grpc.aio.ServicerContext,
        *,
        start: bool,
    ) -> pb.TaskSummaryMessage:
        """执行单个 Task 启停并统一错误映射。"""
        try:
            task_id = _required(request.task_id, "task_id")
            if start:
                await self._require_start_authority(
                    task_id,
                    int(request.placement_generation),
                    context,
                )
            operation = (
                self._runtime.tasks.start_task
                if start
                else self._runtime.tasks.stop_task
            )
            item = await operation(task_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task: {exc.args[0]}")
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _task_summary_to_proto(item)

    async def StartTaskInstance(
        self,
        request: pb.TaskInstanceStartRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskInstanceMessage:
        """启动单个 Task Instance。"""
        return await self._set_task_instance(request, context, start=True)

    async def StopTaskInstance(
        self,
        request: pb.InstanceIdRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.TaskInstanceMessage:
        """停止单个 Task Instance。"""
        return await self._set_task_instance(request, context, start=False)

    async def _set_task_instance(
        self,
        request: pb.InstanceIdRequest,
        context: grpc.aio.ServicerContext,
        *,
        start: bool,
    ) -> pb.TaskInstanceMessage:
        """执行单个 Task Instance 启停并统一错误映射。"""
        try:
            instance_id = _required(request.instance_id, "instance_id")
            if start:
                current = await self._runtime.tasks.get_instance(instance_id)
                await self._require_start_authority(
                    current.task_id,
                    int(request.placement_generation),
                    context,
                )
                item = await self._runtime.tasks.start_instance(instance_id)
            else:
                item = await self._runtime.tasks.stop_instance(instance_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"unknown task instance: {exc.args[0]}",
            )
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _task_instance_to_proto(item)

    async def PrepareConfig(
        self,
        request: pb.PrepareConfigRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PrepareConfigResponse:
        """加载并保存候选配置，不修改当前 Runtime。"""
        try:
            revision_id = _required(request.revision_id, "revision_id")
            config_hash = _required(request.config_hash, "config_hash")
            result = await self._runtime.config.prepare_config(
                revision_id,
                expected_config_hash=config_hash,
                force_reconfigure=request.force_reconfigure,
            )
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        return pb.PrepareConfigResponse(
            success=result.success,
            revision_id=revision_id,
            config_hash=self._runtime.config.prepared_hash or "",
            diff=_diff_to_proto(result.diff),
            errors=list(result.errors),
            duration_ms=result.duration_ms,
        )

    async def ActivateConfig(
        self,
        request: pb.ActivateConfigRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.ActivateConfigResponse:
        """激活指定 prepared revision。"""
        try:
            revision_id = _required(request.revision_id, "revision_id")
            result = await self._runtime.config.activate_config(revision_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        return pb.ActivateConfigResponse(
            success=result.success,
            revision_id=revision_id,
            active_config_hash=self._runtime.config.config_hash,
            diff=_diff_to_proto(result.diff),
            errors=list(result.errors),
            duration_ms=result.duration_ms,
        )

    async def AbortConfig(
        self,
        request: pb.AbortConfigRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.AbortConfigResponse:
        """幂等撤销指定 prepared revision。"""
        try:
            revision_id = _required(request.revision_id, "revision_id")
            aborted = await self._runtime.config.abort_config(revision_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        return pb.AbortConfigResponse(
            success=True,
            revision_id=revision_id,
            aborted=aborted,
        )


def build_grpc_server(
    runtime: AssembledRuntime,
    identity: CollectorIdentity,
    *,
    host: str,
    port: int,
) -> CollectorGrpcServer:
    """构建 Collector gRPC Server，但不开始监听。"""
    server = grpc.aio.server()
    pb_grpc.add_CollectorRuntimeServiceServicer_to_server(
        CollectorRuntimeService(runtime, identity),
        server,
    )
    pb_grpc.add_CollectorControlServiceServicer_to_server(
        CollectorControlService(runtime, identity),
        server,
    )
    requested_endpoint = f"{host}:{port}"
    bound_port = server.add_insecure_port(requested_endpoint)
    if bound_port == 0:
        raise RuntimeError(
            f"failed to bind Collector gRPC endpoint {requested_endpoint}"
        )
    return CollectorGrpcServer(server=server, endpoint=f"{host}:{bound_port}")
