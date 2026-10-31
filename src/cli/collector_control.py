"""Collector v2 control commands for the interactive CLI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import grpc
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict, ParseDict

from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as rpc


def _selector(scope: str, name: str, version: str = "") -> pb.ConfigSelector:
    try:
        selection = getattr(pb, scope.upper())
    except AttributeError as exc:
        raise ValueError("scope must be SERVICE, INSTANCE or TEMPLATE") from exc
    return pb.ConfigSelector(scope=selection, name=name, version=version)


async def execute(args: list[str]) -> dict[str, object]:
    """Run one typed Collector v2 request without changing the CLI config session."""
    if not args:
        raise ValueError("usage: collector OP [DEVICE_ID] [--target HOST:PORT]")
    target = "127.0.0.1:50051"
    if "--target" in args:
        pos = args.index("--target")
        if pos + 1 >= len(args):
            raise ValueError("--target requires HOST:PORT")
        target = args[pos + 1]
        args = args[:pos] + args[pos + 2 :]
    action, *params = args
    token = os.environ.get("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
    metadata = (("x-windhub-admin-token", token),) if token else ()
    async with grpc.aio.insecure_channel(target) as channel:
        runtime = rpc.CollectorRuntimeServiceStub(channel)
        tasks = rpc.CollectorTaskServiceStub(channel)
        config = rpc.CollectorConfigServiceStub(channel)
        metrics = rpc.CollectorMetricsServiceStub(channel)
        timeout = 5.0
        if action == "info" and not params:
            reply = await runtime.GetCollectorInfo(empty_pb2.Empty(), timeout=timeout)
        elif action == "status" and not params:
            reply = await runtime.GetRuntimeStatus(empty_pb2.Empty(), timeout=timeout)
        elif action == "devices" and not params:
            reply = await runtime.ListDevices(pb.ListDevicesRequest(), timeout=timeout)
        elif action == "tasks" and not params:
            reply = await tasks.ListTasks(pb.ListTasksRequest(), timeout=timeout)
        elif action == "metrics" and not params:
            reply = await metrics.GetMetrics(pb.GetMetricsRequest(), timeout=timeout)
        elif action == "events" and not params:
            reply = await metrics.GetMetricsEvents(pb.GetMetricsEventsRequest(), timeout=timeout)
        elif action == "config-status" and not params:
            reply = await config.GetConfigStatus(
                pb.GetConfigStatusRequest(), timeout=timeout, metadata=metadata
            )
        elif action in {"device", "task", "latest", "points"} and len(params) >= 1:
            device_id, *point_ids = params
            if action == "device" and not point_ids:
                reply = await runtime.GetDevice(
                    pb.DeviceIdRequest(device_id=device_id), timeout=timeout
                )
            elif action == "task" and not point_ids:
                reply = await tasks.GetTask(
                    pb.DeviceIdRequest(device_id=device_id), timeout=timeout
                )
            elif action == "latest":
                reply = await runtime.GetLatestValues(
                    pb.GetLatestValuesRequest(device_id=device_id, point_ids=point_ids),
                    timeout=timeout,
                )
            elif action == "points":
                reply = await runtime.GetPointMetadata(
                    pb.GetPointMetadataRequest(device_id=device_id, point_ids=point_ids),
                    timeout=timeout,
                )
            else:
                raise ValueError("unexpected point IDs")
        elif action == "config-get" and 2 <= len(params) <= 3:
            reply = await config.GetConfig(
                pb.GetConfigRequest(selector=_selector(*params)),
                timeout=timeout,
                metadata=metadata,
            )
        elif action == "config-validate" and 3 <= len(params) <= 4:
            scope, name, filename, *versions = params
            reply = await config.ValidateConfig(
                pb.ValidateConfigRequest(
                    selector=_selector(scope, name, *versions),
                    yaml_content=Path(filename).read_text(encoding="utf-8"),
                ),
                timeout=timeout,
                metadata=metadata,
            )
        elif action == "config-replace" and 4 <= len(params) <= 5:
            scope, name, revision, filename, *versions = params
            reply = await config.ReplaceConfig(
                pb.ReplaceConfigRequest(
                    selector=_selector(scope, name, *versions),
                    expected_revision=revision,
                    request_id=uuid4().hex,
                    yaml_content=Path(filename).read_text(encoding="utf-8"),
                ),
                timeout=timeout,
                metadata=metadata,
            )
        elif action in {"config-set", "config-remove"} and (
            (action == "config-set" and 5 <= len(params) <= 6)
            or (action == "config-remove" and 4 <= len(params) <= 5)
        ):
            scope, name, revision, pointer, *extra = params
            patch = pb.ConfigPatch(
                path=pointer,
                operation=pb.SET if action == "config-set" else pb.REMOVE,
            )
            if action == "config-set":
                ParseDict(json.loads(extra[0]), patch.value)
                extra = extra[1:]
            reply = await config.UpdateConfig(
                pb.UpdateConfigRequest(
                    selector=_selector(scope, name, *extra),
                    expected_revision=revision,
                    request_id=uuid4().hex,
                    updates=[patch],
                ),
                timeout=timeout,
                metadata=metadata,
            )
        elif action in {"start", "stop"} and len(params) == 2:
            device_id, revision = params
            method = tasks.StartTask if action == "start" else tasks.StopTask
            reply = await method(
                pb.TaskChangeRequest(
                    device_id=device_id,
                    expected_revision=revision,
                    request_id=uuid4().hex,
                ),
                timeout=timeout,
                metadata=metadata,
            )
        else:
            raise ValueError(
                "usage: collector info|status|devices|tasks|metrics|events|"
                "config-status|device ID|task ID|latest ID [POINT...]|"
                "points ID [POINT...]|start ID REVISION|stop ID REVISION"
            )
        return MessageToDict(reply, preserving_proto_field_name=True)


def run(args: list[str]) -> dict[str, object]:
    """Execute a Collector v2 request from sync cmd.Cmd dispatch."""
    try:
        return asyncio.run(execute(args))
    except grpc.aio.AioRpcError as exc:
        raise RuntimeError(f"Collector RPC {exc.code().name}: {exc.details()}") from exc
