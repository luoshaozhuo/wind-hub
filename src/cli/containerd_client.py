"""Minimal native containerd Tasks gRPC client over an allowlisted Unix socket."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.empty_pb2 import Empty

from .runtime import ServiceTarget


def _messages() -> dict[str, type[Any]]:
    file = descriptor_pb2.FileDescriptorProto()
    file.name = "windhub_containerd_tasks_subset.proto"
    file.package = "windhub.containerd.client"
    file.syntax = "proto3"

    def message(name: str, fields: list[tuple[str, int, int, str]]) -> None:
        entry = file.message_type.add()
        entry.name = name
        for field_name, field_number, field_type, type_name in fields:
            field = entry.field.add()
            field.name = field_name
            field.number = field_number
            field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
            field.type = field_type  # type: ignore[assignment]  # stubs type Field.type as closed enum
            if type_name:
                field.type_name = type_name

    string = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    uint32 = descriptor_pb2.FieldDescriptorProto.TYPE_UINT32
    boolean = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    nested = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    message(
        "Process",
        [
            ("container_id", 1, string, ""),
            # containerd v1.7 Tasks/Get leaves container_id empty on the wire and
            # returns the init process ID (which equals the container ID).
            ("id", 2, string, ""),
            ("pid", 3, uint32, ""),
            ("status", 4, uint32, ""),
        ],
    )
    message("GetRequest", [("container_id", 1, string, "")])
    message(
        "GetResponse",
        [
            ("process", 1, nested, ".windhub.containerd.client.Process"),
        ],
    )
    message("StartRequest", [("container_id", 1, string, "")])
    message("StartResponse", [("pid", 1, uint32, "")])
    message(
        "KillRequest",
        [
            ("container_id", 1, string, ""),
            ("signal", 3, uint32, ""),
            ("all", 4, boolean, ""),
        ],
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return {
        name: message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"windhub.containerd.client.{name}")
        )
        for name in ("GetRequest", "GetResponse", "StartRequest", "StartResponse", "KillRequest")
    }


_MESSAGES = _messages()
_BASE = "/containerd.services.tasks.v1.Tasks"
_STATUS = {0: "UNKNOWN", 1: "CREATED", 2: "RUNNING", 3: "STOPPED", 4: "PAUSED", 5: "PAUSING"}


async def containerd_manage(target: ServiceTarget, action: str) -> dict[str, Any]:
    """Only Tasks API is used. Do not claim to create a missing OCI task."""
    if not target.manager_socket or not target.namespace or not target.target:
        raise ValueError("containerd requires socket, namespace and container target")
    socket = target.manager_socket
    if not socket.startswith("/"):
        raise ValueError("containerd socket must be an absolute path")
    metadata = (("containerd-namespace", target.namespace),)
    async with grpc.aio.insecure_channel(f"unix://{socket}") as channel:

        def method(name: str, response_name: str) -> Any:
            response_cls = _MESSAGES[response_name] if response_name != "Empty" else Empty
            return channel.unary_unary(
                f"{_BASE}/{name}",
                request_serializer=lambda request: request.SerializeToString(),
                response_deserializer=response_cls.FromString,
            )

        async def status(deadline: float | None = None) -> dict[str, Any]:
            timeout = target.timeout
            if deadline is not None:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    raise TimeoutError("containerd restart deadline expired")
            request = _MESSAGES["GetRequest"](container_id=target.target)
            response = await method("Get", "GetResponse")(
                request,
                metadata=metadata,
                timeout=timeout,
            )
            process = response.process
            identities = {value for value in (process.id, process.container_id) if value}
            if target.target not in identities:
                raise RuntimeError("containerd Get returned an unexpected task")
            return {
                "manager": "containerd",
                "container_id": target.target,
                "pid": int(process.pid),
                "state": _STATUS.get(int(process.status), "UNKNOWN"),
            }

        if action == "status":
            return await status()
        if action == "start":
            current = await status()
            if current["state"] == "RUNNING":
                return {**current, "already_running": True}
            if current["state"] != "CREATED":
                raise RuntimeError("containerd Start requires a CREATED task")
            request = _MESSAGES["StartRequest"](container_id=target.target)
            response = await method("Start", "StartResponse")(
                request,
                metadata=metadata,
                timeout=target.timeout,
            )
            result = await status()
            result["start_pid"] = int(response.pid)
            if result["state"] != "RUNNING":
                raise RuntimeError("containerd Start was not confirmed RUNNING")
            return result
        if action == "restart":
            from .containerd_restart import restart_task

            deadline = asyncio.get_running_loop().time() + target.timeout
            prepared = await restart_task(
                channel,
                target,
                metadata,
                preview_only=True,
                deadline=deadline,
            )
            stages: list[dict[str, Any]] = [
                {"operation": "preflight", "confirmed": True},
            ]
            current = await status(deadline)
            if current["state"] in ("RUNNING", "PAUSED", "PAUSING"):
                request = _MESSAGES["KillRequest"](
                    container_id=target.target,
                    signal=int(signal.SIGTERM),
                    all=True,
                )
                await method("Kill", "Empty")(
                    request,
                    metadata=metadata,
                    timeout=max(0.01, deadline - asyncio.get_running_loop().time()),
                )
                while asyncio.get_running_loop().time() < deadline:
                    current = await status(deadline)
                    if current["state"] == "STOPPED":
                        stages.append({"operation": "stop", "confirmed": True})
                        break
                    remaining = max(0, deadline - asyncio.get_running_loop().time())
                    await asyncio.sleep(min(0.2, remaining))
                else:
                    raise TimeoutError("containerd restart stop not confirmed")
            if current["state"] != "STOPPED":
                raise RuntimeError("containerd restart requires a STOPPED task")
            if not any(stage["operation"] == "stop" for stage in stages):
                stages.append({"operation": "already_stopped", "confirmed": True})
            await restart_task(
                channel,
                target,
                metadata,
                prepared=prepared,
                deadline=deadline,
            )
            stages.append({"operation": "delete_create_start", "confirmed": True})
            while asyncio.get_running_loop().time() < deadline:
                current = await status(deadline)
                if current["state"] == "RUNNING":
                    stages.append({"operation": "running", "confirmed": True})
                    return {**current, "confirmed": True, "stages": stages}
                if current["state"] == "STOPPED":
                    raise RuntimeError("containerd task stopped immediately after restart")
                await asyncio.sleep(min(0.2, max(0, deadline - asyncio.get_running_loop().time())))
            raise TimeoutError("containerd restart RUNNING state not confirmed")
        if action == "stop":
            current = await status()
            if current["state"] == "STOPPED":
                return {**current, "already_stopped": True}
            if current["state"] not in ("RUNNING", "PAUSED", "PAUSING"):
                raise RuntimeError("containerd Kill requires an active task")
            request = _MESSAGES["KillRequest"](
                container_id=target.target,
                signal=int(signal.SIGTERM),
                all=True,
            )
            await method("Kill", "Empty")(
                request,
                metadata=metadata,
                timeout=target.timeout,
            )
            deadline = asyncio.get_running_loop().time() + target.timeout
            while asyncio.get_running_loop().time() < deadline:
                result = await status()
                if result["state"] == "STOPPED":
                    return result
                await asyncio.sleep(0.2)
            raise TimeoutError("containerd task stop not confirmed")
    raise ValueError(f"unsupported containerd operation: {action}")
