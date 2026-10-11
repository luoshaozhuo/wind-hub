"""Collector / Commander 的直接 gRPC 客户端。"""

from __future__ import annotations

from typing import Any

import grpc
from google.protobuf import empty_pb2, message
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.timestamp_pb2 import Timestamp
from google.protobuf.wrappers_pb2 import BoolValue, DoubleValue, StringValue

from core.infrastructure.rpc import collector_pb2 as collector_pb
from core.infrastructure.rpc import collector_pb2_grpc as collector_grpc
from core.infrastructure.rpc import commander_pb2 as commander_pb
from core.infrastructure.rpc import commander_pb2_grpc as commander_grpc


def _field_value(value: object) -> object:
    """把 protobuf 字段值转换为 JSON 可序列化 Python 值。"""
    if isinstance(value, Timestamp):
        return value.ToJsonString()
    if isinstance(value, (BoolValue, DoubleValue, StringValue)):
        return value.value
    if isinstance(value, message.Message):
        return _to_dict(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _to_dict(value: message.Message) -> dict[str, Any]:
    """按 protobuf 描述符转换，保留 64 位整数的 Python int 语义。"""
    result: dict[str, Any] = {}
    for field in value.DESCRIPTOR.fields:
        if field.containing_oneof is not None:
            selected = value.WhichOneof(field.containing_oneof.name)
            if selected != field.name:
                continue

        field_value = getattr(value, field.name)
        if field.is_repeated:
            result[field.name] = [_field_value(item) for item in field_value]
            continue

        if field.cpp_type == FieldDescriptor.CPPTYPE_MESSAGE:
            if not value.HasField(field.name):
                continue
            result[field.name] = _field_value(field_value)
            continue

        result[field.name] = _field_value(field_value)
    return result


def encode_scalar(value: object) -> commander_pb.ScalarValue:
    """把 CLI 标量编码为 Commander ScalarValue。"""
    result = commander_pb.ScalarValue()
    if isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        if -(2**63) <= value <= 2**63 - 1:
            result.sint64_value = value
        elif 0 <= value <= 2**64 - 1:
            result.uint64_value = value
        else:
            raise ValueError("integer is outside protobuf 64-bit range")
    elif isinstance(value, float):
        result.double_value = value
    elif isinstance(value, str):
        result.string_value = value
    elif isinstance(value, bytes):
        result.bytes_value = value
    else:
        raise ValueError(f"unsupported scalar type: {type(value).__name__}")
    return result


class CollectorClient:
    """Collector gRPC 直接客户端。"""

    def __init__(self, target: str, *, timeout: float) -> None:
        self._target = target
        self._timeout = timeout
        self._channel: grpc.aio.Channel | None = None
        self._runtime: collector_grpc.CollectorRuntimeServiceStub | None = None
        self._control: collector_grpc.CollectorControlServiceStub | None = None

    async def __aenter__(self) -> "CollectorClient":
        self._channel = grpc.aio.insecure_channel(self._target)
        self._runtime = collector_grpc.CollectorRuntimeServiceStub(self._channel)
        self._control = collector_grpc.CollectorControlServiceStub(self._channel)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._runtime = None
        self._control = None

    def runtime(self) -> collector_grpc.CollectorRuntimeServiceStub:
        """返回 Runtime stub。"""
        if self._runtime is None:
            raise RuntimeError("CollectorClient must be used as an async context manager")
        return self._runtime

    def control(self) -> collector_grpc.CollectorControlServiceStub:
        """返回 Control stub。"""
        if self._control is None:
            raise RuntimeError("CollectorClient must be used as an async context manager")
        return self._control

    async def call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        """执行一个 Collector 操作。"""
        runtime = self.runtime()
        control = self.control()
        timeout = self._timeout

        if operation == "info":
            response = await runtime.GetCollectorInfo(empty_pb2.Empty(), timeout=timeout)
        elif operation == "status":
            response = await runtime.GetRuntimeStatus(empty_pb2.Empty(), timeout=timeout)
        elif operation == "metrics":
            response = await runtime.GetMetricsSnapshot(empty_pb2.Empty(), timeout=timeout)
        elif operation == "devices":
            response = await runtime.ListDevices(empty_pb2.Empty(), timeout=timeout)
        elif operation == "sinks":
            response = await runtime.ListSinks(empty_pb2.Empty(), timeout=timeout)
        elif operation == "verify-sink":
            response = await runtime.VerifySink(
                collector_pb.SinkRequest(name=kwargs["name"]),
                timeout=timeout,
            )
        elif operation == "test-sink":
            response = await runtime.WriteTestSink(
                collector_pb.SinkRequest(name=kwargs["name"]),
                timeout=timeout,
            )
        elif operation == "tasks":
            response = await runtime.ListTasks(empty_pb2.Empty(), timeout=timeout)
        elif operation == "task":
            response = await runtime.GetTask(
                collector_pb.TaskIdRequest(task_id=kwargs["task_id"]),
                timeout=timeout,
            )
        elif operation == "instances":
            response = await runtime.ListTaskInstances(empty_pb2.Empty(), timeout=timeout)
        elif operation == "instance":
            response = await runtime.GetTaskInstance(
                collector_pb.InstanceIdRequest(instance_id=kwargs["instance_id"]),
                timeout=timeout,
            )
        elif operation == "placement":
            response = await control.ApplyTaskPlacement(
                collector_pb.TaskPlacementSnapshotRequest(
                    worker_id=kwargs["worker_id"],
                    generation=kwargs["generation"],
                    task_ids=kwargs["task_ids"],
                ),
                timeout=timeout,
            )
        elif operation == "start-task":
            response = await control.StartTask(
                collector_pb.TaskStartRequest(
                    task_id=kwargs["task_id"],
                    placement_generation=kwargs["generation"],
                ),
                timeout=timeout,
            )
        elif operation == "stop-task":
            response = await control.StopTask(
                collector_pb.TaskIdRequest(task_id=kwargs["task_id"]),
                timeout=timeout,
            )
        elif operation == "start-instance":
            response = await control.StartTaskInstance(
                collector_pb.TaskInstanceStartRequest(
                    instance_id=kwargs["instance_id"],
                    placement_generation=kwargs["generation"],
                ),
                timeout=timeout,
            )
        elif operation == "stop-instance":
            response = await control.StopTaskInstance(
                collector_pb.InstanceIdRequest(instance_id=kwargs["instance_id"]),
                timeout=timeout,
            )
        elif operation == "prepare-config":
            response = await control.PrepareConfig(
                collector_pb.PrepareConfigRequest(
                    revision_id=kwargs["revision_id"],
                    config_hash=kwargs["config_hash"],
                    force_reconfigure=kwargs["force"],
                ),
                timeout=timeout,
            )
        elif operation == "activate-config":
            response = await control.ActivateConfig(
                collector_pb.ActivateConfigRequest(revision_id=kwargs["revision_id"]),
                timeout=timeout,
            )
        elif operation == "abort-config":
            response = await control.AbortConfig(
                collector_pb.AbortConfigRequest(revision_id=kwargs["revision_id"]),
                timeout=timeout,
            )
        else:
            raise ValueError(f"unsupported collector operation: {operation}")

        return _to_dict(response)


class CommanderClient:
    """Commander gRPC 直接客户端。"""

    def __init__(self, target: str, *, timeout: float) -> None:
        self._target = target
        self._timeout = timeout
        self._channel: grpc.aio.Channel | None = None
        self._stub: commander_grpc.CommanderServiceStub | None = None

    async def __aenter__(self) -> "CommanderClient":
        self._channel = grpc.aio.insecure_channel(self._target)
        self._stub = commander_grpc.CommanderServiceStub(self._channel)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._stub = None

    def stub(self) -> commander_grpc.CommanderServiceStub:
        """返回 Commander stub。"""
        if self._stub is None:
            raise RuntimeError("CommanderClient must be used as an async context manager")
        return self._stub

    async def call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        """执行一个 Commander 操作。"""
        stub = self.stub()
        timeout = self._timeout

        if operation == "status":
            response = await stub.GetStatus(empty_pb2.Empty(), timeout=timeout)
        elif operation == "devices":
            response = await stub.ListDevices(empty_pb2.Empty(), timeout=timeout)
        elif operation == "read":
            response = await stub.ReadPoint(
                commander_pb.ReadPointRequest(
                    device_id=kwargs["device_id"],
                    point_id=kwargs["point_id"],
                ),
                timeout=timeout,
            )
        elif operation == "read-many":
            response = await stub.ReadPoints(
                commander_pb.ReadPointsRequest(
                    device_id=kwargs["device_id"],
                    point_ids=kwargs["point_ids"],
                ),
                timeout=timeout,
            )
        elif operation == "write":
            response = await stub.WritePoint(
                commander_pb.WritePointRequest(
                    command_id=kwargs["command_id"],
                    device_id=kwargs["device_id"],
                    point_id=kwargs["point_id"],
                    value=encode_scalar(kwargs["value"]),
                    timeout=kwargs["io_timeout"],
                ),
                timeout=timeout,
            )
        elif operation == "write-many":
            commands = [
                commander_pb.WritePointRequest(
                    command_id=item["command_id"],
                    device_id=item["device_id"],
                    point_id=item["point_id"],
                    value=encode_scalar(item["value"]),
                    timeout=item["timeout"],
                )
                for item in kwargs["items"]
            ]
            response = await stub.WritePoints(
                commander_pb.WritePointsRequest(commands=commands),
                timeout=timeout,
            )
        elif operation == "verify-device":
            response = await stub.VerifyDevice(
                commander_pb.VerifyDeviceRequest(
                    device_id=kwargs["device_id"],
                    timeout=kwargs["io_timeout"],
                ),
                timeout=timeout,
            )
        elif operation == "resolve-point":
            response = await stub.ResolvePoint(
                commander_pb.PointRequest(
                    device_id=kwargs["device_id"],
                    point_id=kwargs["point_id"],
                ),
                timeout=timeout,
            )
        elif operation == "verify-point":
            response = await stub.VerifyPoint(
                commander_pb.PointRequest(
                    device_id=kwargs["device_id"],
                    point_id=kwargs["point_id"],
                ),
                timeout=timeout,
            )
        elif operation == "verify-points":
            response = await stub.VerifyPoints(
                commander_pb.VerifyPointsRequest(
                    device_id=kwargs["device_id"],
                    point_group=kwargs["point_group"],
                ),
                timeout=timeout,
            )
        elif operation == "prepare-config":
            response = await stub.PrepareConfig(
                commander_pb.PrepareConfigRequest(
                    revision_id=kwargs["revision_id"],
                    config_hash=kwargs["config_hash"],
                ),
                timeout=timeout,
            )
        elif operation == "activate-config":
            response = await stub.ActivateConfig(
                commander_pb.ActivateConfigRequest(revision_id=kwargs["revision_id"]),
                timeout=timeout,
            )
        elif operation == "abort-config":
            response = await stub.AbortConfig(
                commander_pb.AbortConfigRequest(revision_id=kwargs["revision_id"]),
                timeout=timeout,
            )
        else:
            raise ValueError(f"unsupported commander operation: {operation}")

        return _to_dict(response)
