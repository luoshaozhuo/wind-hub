"""Reconstruct one configured containerd Task using native gRPC APIs.

The Container metadata and active snapshot are read before destroying a task.
This adapter supports existing OCI containers with an attached snapshot only.
It intentionally does not retry a Create or Start with an unknown result.
"""

from __future__ import annotations

import asyncio
import os
import stat
from typing import Any

import grpc
from google.protobuf import any_pb2, descriptor_pb2, descriptor_pool, message_factory

from .runtime import ServiceTarget


def _types() -> dict[str, type[Any]]:
    file = descriptor_pb2.FileDescriptorProto()
    file.name = "windhub_containerd_restart_subset.proto"
    file.package = "windhub.containerd.restart"
    file.syntax = "proto3"
    file.dependency.append("google/protobuf/any.proto")
    string = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    uint32 = descriptor_pb2.FieldDescriptorProto.TYPE_UINT32
    boolean = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    message_type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE

    def define(name: str, fields: list[tuple[str, int, int, str, bool]]) -> None:
        record = file.message_type.add()
        record.name = name
        for key, index, dtype, type_name, repeated in fields:
            field = record.field.add()
            field.name = key
            field.number = index
            field.label = (
                descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
                if repeated
                else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
            )
            field.type = dtype  # type: ignore[assignment]  # stubs type Field.type as closed enum
            if type_name:
                field.type_name = type_name

    define(
        "Runtime",
        [
            ("name", 1, string, "", False),
            ("options", 2, message_type, ".google.protobuf.Any", False),
        ],
    )
    define(
        "Container",
        [
            ("id", 1, string, "", False),
            ("runtime", 4, message_type, ".windhub.containerd.restart.Runtime", False),
            ("snapshotter", 6, string, "", False),
            ("snapshot_key", 7, string, "", False),
        ],
    )
    define("GetContainerRequest", [("id", 1, string, "", False)])
    define(
        "GetContainerResponse",
        [
            ("container", 1, message_type, ".windhub.containerd.restart.Container", False),
        ],
    )
    define(
        "Mount",
        [
            ("type", 1, string, "", False),
            ("source", 2, string, "", False),
            ("target", 3, string, "", False),
            ("options", 4, string, "", True),
        ],
    )
    define(
        "MountsRequest",
        [
            ("snapshotter", 1, string, "", False),
            ("key", 2, string, "", False),
        ],
    )
    define(
        "MountsResponse",
        [
            ("mounts", 1, message_type, ".windhub.containerd.restart.Mount", True),
        ],
    )
    define(
        "TaskProcess",
        [
            ("container_id", 1, string, "", False),
            # containerd v1.7 returns the init process ID (== container ID) here.
            ("id", 2, string, "", False),
            ("stdin", 5, string, "", False),
            ("stdout", 6, string, "", False),
            ("stderr", 7, string, "", False),
            ("terminal", 8, boolean, "", False),
        ],
    )
    define("GetTaskRequest", [("container_id", 1, string, "", False)])
    define(
        "GetTaskResponse",
        [("process", 1, message_type, ".windhub.containerd.restart.TaskProcess", False)],
    )
    define("DeleteTaskRequest", [("container_id", 1, string, "", False)])
    define("StartRequest", [("container_id", 1, string, "", False)])
    define(
        "DeleteResponse",
        [
            ("id", 1, string, "", False),
            ("pid", 2, uint32, "", False),
            ("exit_status", 3, uint32, "", False),
        ],
    )
    define(
        "CreateTaskRequest",
        [
            ("container_id", 1, string, "", False),
            ("rootfs", 3, message_type, ".windhub.containerd.restart.Mount", True),
            ("stdin", 4, string, "", False),
            ("stdout", 5, string, "", False),
            ("stderr", 6, string, "", False),
            ("terminal", 7, boolean, "", False),
            ("options", 9, message_type, ".google.protobuf.Any", False),
        ],
    )
    define(
        "CreateTaskResponse",
        [
            ("container_id", 1, string, "", False),
            ("pid", 2, uint32, "", False),
        ],
    )
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(any_pb2.DESCRIPTOR.serialized_pb)
    pool.Add(file)
    names = (
        "GetContainerRequest",
        "GetContainerResponse",
        "MountsRequest",
        "MountsResponse",
        "DeleteTaskRequest",
        "DeleteResponse",
        "CreateTaskRequest",
        "CreateTaskResponse",
        "GetTaskRequest",
        "GetTaskResponse",
        "StartRequest",
    )
    return {
        name: message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"windhub.containerd.restart.{name}")
        )
        for name in names
    }


_MESSAGES = _types()


async def restart_task(
    channel: grpc.aio.Channel,
    target: ServiceTarget,
    metadata: tuple[tuple[str, str], ...],
    *,
    prepared: Any | None = None,
    preview_only: bool = False,
    deadline: float | None = None,
) -> Any:
    """Delete a stopped task, reconstruct its OCI rootfs, then Start it once."""
    if not target.target:
        raise ValueError("containerd target missing")
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + target.timeout

    def call(service: str, method: str, result_type: str) -> Any:
        return channel.unary_unary(
            f"/containerd.services.{service}.v1.{service.capitalize()}/{method}",
            request_serializer=lambda request: request.SerializeToString(),
            response_deserializer=_MESSAGES[result_type].FromString,
        )

    # Containers service is plural with a matching plural class name.
    get_container = call("containers", "Get", "GetContainerResponse")
    get_task = call("tasks", "Get", "GetTaskResponse")
    mounts = call("snapshots", "Mounts", "MountsResponse")
    delete = call("tasks", "Delete", "DeleteResponse")
    create = call("tasks", "Create", "CreateTaskResponse")
    start: grpc.aio.UnaryUnaryMultiCallable[Any, Any] = channel.unary_unary(
        "/containerd.services.tasks.v1.Tasks/Start",
        request_serializer=lambda request: request.SerializeToString(),
        response_deserializer=lambda value: value,
    )

    async def request(method: Any, payload: Any) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("containerd restart deadline expired")
        return await method(payload, metadata=metadata, timeout=remaining)

    if prepared is None:
        container_response = await request(
            get_container, _MESSAGES["GetContainerRequest"](id=target.target)
        )
        container = container_response.container
        if container.id != target.target or not container.snapshotter or not container.snapshot_key:
            raise ValueError("containerd restart requires a verified attached snapshot")
        if not container.runtime.name:
            raise ValueError("containerd restart requires stored OCI runtime metadata")

        rootfs = await request(
            mounts,
            _MESSAGES["MountsRequest"](
                snapshotter=container.snapshotter,
                key=container.snapshot_key,
            ),
        )
        if not rootfs.mounts:
            raise ValueError("containerd restart requires nonempty rootfs mounts")

        task_response = await request(
            get_task, _MESSAGES["GetTaskRequest"](container_id=target.target)
        )
        process = task_response.process
        identities = {value for value in (process.id, process.container_id) if value}
        if target.target not in identities:
            raise RuntimeError("containerd Task.Get returned mismatched container ID")
        for stream_name in ("stdin", "stdout", "stderr"):
            stream_path = getattr(process, stream_name)
            if not stream_path:
                continue
            if not os.path.isabs(stream_path):
                raise RuntimeError(f"containerd {stream_name} path is not absolute")
            try:
                mode = os.stat(stream_path).st_mode
            except OSError as exc:
                raise RuntimeError(
                    f"containerd original {stream_name} stream is unavailable"
                ) from exc
            if not stat.S_ISFIFO(mode):
                raise RuntimeError(f"containerd original {stream_name} is not a FIFO")
        # A FIFO is a live transport, not a reusable configuration path.
        # Reopening its pathname after Tasks.Delete can block indefinitely
        # without the original reader/writer. Never delete the existing task
        # until a fresh, owned stdio relay can be constructed.
        if any(getattr(process, name) for name in ("stdin", "stdout", "stderr")):
            raise RuntimeError(
                "containerd restart with FIFO stdio requires a managed relay; "
                "refusing to delete the existing task"
            )
        create_request = _MESSAGES["CreateTaskRequest"](container_id=target.target)
        create_request.rootfs.extend(rootfs.mounts)
        create_request.stdin = process.stdin
        create_request.stdout = process.stdout
        create_request.stderr = process.stderr
        create_request.terminal = process.terminal
        if container.runtime.HasField("options"):
            create_request.options.CopyFrom(container.runtime.options)

    else:
        create_request = prepared
    if preview_only:
        return create_request

    # Preflight is complete; subsequent failure may leave the task absent or stopped.
    # Never retry an ambiguous Create/Start automatically.
    await request(delete, _MESSAGES["DeleteTaskRequest"](container_id=target.target))
    created = await request(create, create_request)
    if created.container_id != target.target:
        raise RuntimeError("containerd Create returned a different container ID")
    await request(start, _MESSAGES["StartRequest"](container_id=target.target))
    return {
        "manager": "containerd",
        "container_id": target.target,
        "status": "START_REQUESTED",
        "confirmed": False,
    }
