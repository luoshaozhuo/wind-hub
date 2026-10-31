"""System / Reliability 层的 Collector v2 控制面 RPC helper。

system 测试需要驱动 Task 生命周期时，直接以 gRPC 调用 Collector v2
四个服务——与 CLI → Collector 的生产控制路径一致；不经过任何 Server
中转，也不使用任何 v1 兼容层：

- 生命周期以 **设备** 为粒度（StartTask/StopTask），携带 devices 文档
  当前 revision（expected_revision）与幂等 request_id；
- START/STOP 意图由 Collector 持久化到 devices.yaml，冷启动按意图恢复；
- 配置经五个 Config RPC 读取/校验/替换（无 Prepare/Activate 两段事务）；
- 指标为累计计数 + 有界诊断事件（GetMetrics/GetMetricsEvents）。

进程必须以同一管理员令牌启动（WIND_HUB_COLLECTOR_ADMIN_TOKEN）。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import grpc
import yaml

from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc

#: system harness 启动 Collector 时统一注入的测试管理员令牌。
DEFAULT_ADMIN_TOKEN = "system-test-admin-token"

_RUNTIME = pb.RuntimeStatusResponse
_TASK_ACTUAL_NAMES = {
    pb.RUNNING: "RUNNING",
    pb.STOPPED: "STOPPED",
}


def _admin_token(token: str | None) -> str:
    return token or os.environ.get("WIND_HUB_COLLECTOR_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)


def _metadata(token: str | None) -> tuple[tuple[str, str]]:
    return (("x-windhub-admin-token", _admin_token(token)),)  # type: ignore[return-value]


def _devices_selector() -> pb.ConfigSelector:
    return pb.ConfigSelector(scope=pb.INSTANCE, name="devices")


def _template_selector(category: str, template_id: str, version: str) -> pb.ConfigSelector:
    """TEMPLATE 选择器：name 必须是 ``category/id``，且携带精确版本。"""
    return pb.ConfigSelector(scope=pb.TEMPLATE, name=f"{category}/{template_id}", version=version)


def _channel(target: str) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(target)


def device_of_instance(instance_id: str) -> str:
    """v1 风格 ``{task_id}:{device_id}`` → v2 设备粒度 ID。"""
    return instance_id.rpartition(":")[2] or instance_id


async def collector_info(target: str, *, timeout: float = 5.0) -> pb.CollectorInfoResponse:
    """Collector 身份与活跃配置代次（GetCollectorInfo）。"""
    from google.protobuf import empty_pb2

    async with _channel(target) as channel:
        return await pb_grpc.CollectorRuntimeServiceStub(channel).GetCollectorInfo(
            empty_pb2.Empty(), timeout=timeout
        )


async def runtime_status(target: str, *, timeout: float = 5.0) -> _RUNTIME:
    """Runtime 运行状态（GetRuntimeStatus）。"""
    from google.protobuf import empty_pb2

    async with _channel(target) as channel:
        return await pb_grpc.CollectorRuntimeServiceStub(channel).GetRuntimeStatus(
            empty_pb2.Empty(), timeout=timeout
        )


async def runtime_status_snapshot(target: str) -> dict[str, Any] | None:
    """v2 Runtime 状态的 dict 快照；gRPC 暂不可用时返回 None（供轮询）。"""
    try:
        status = await runtime_status(target)
    except grpc.aio.AioRpcError:
        return None
    return {
        "running": status.running,
        "device_total": status.device_total,
        "device_connected": status.device_connected,
        "device_disconnected": status.device_disconnected,
        "task_total": status.task_total,
        "task_running": status.task_running,
        "task_stopped": status.task_stopped,
        "nats_connected": status.nats_connected,
        "config_status": pb.ConfigStatus.Name(status.config_status),
    }


async def config_revision(target: str, *, token: str | None = None, timeout: float = 5.0) -> str:
    """读取 devices 文档当前 revision（START/STOP/配置变更的乐观版本）。"""
    async with _channel(target) as channel:
        response = await pb_grpc.CollectorConfigServiceStub(channel).GetConfig(
            pb.GetConfigRequest(selector=_devices_selector()),
            metadata=_metadata(token),
            timeout=timeout,
        )
        return response.revision


async def start_task(
    target: str,
    device_id: str,
    *,
    token: str | None = None,
    expected_revision: str | None = None,
    timeout: float = 10.0,
) -> pb.TaskChangeResponse:
    """设备级 START：持久化意图并收敛到 RUNNING（需管理员令牌）。"""
    revision = expected_revision or await config_revision(target, token=token)
    async with _channel(target) as channel:
        return await pb_grpc.CollectorTaskServiceStub(channel).StartTask(
            pb.TaskChangeRequest(
                device_id=device_id,
                expected_revision=revision,
                request_id=f"start-{device_id}-{uuid.uuid4().hex[:12]}",
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )


async def stop_task(
    target: str,
    device_id: str,
    *,
    token: str | None = None,
    expected_revision: str | None = None,
    timeout: float = 10.0,
) -> pb.TaskChangeResponse:
    """设备级 STOP（幂等意图，持久化；手动 STOP 后看门狗不得自动复活）。"""
    revision = expected_revision or await config_revision(target, token=token)
    async with _channel(target) as channel:
        return await pb_grpc.CollectorTaskServiceStub(channel).StopTask(
            pb.TaskChangeRequest(
                device_id=device_id,
                expected_revision=revision,
                request_id=f"stop-{device_id}-{uuid.uuid4().hex[:12]}",
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )


async def start_task_instance(
    target: str,
    *,
    task_id: str = "",  # noqa: ARG001  # v2 设备粒度，task_id 仅为迁移期签名兼容
    instance_id: str,
    placement_generation: int = 1,  # noqa: ARG001  # v1 placement 栅栏已移除
    token: str | None = None,
) -> pb.TaskChangeResponse:
    """迁移期适配：v1 ``instance_id="{task}:{device}"`` → v2 设备级 StartTask。"""
    return await start_task(target, device_of_instance(instance_id), token=token)


async def stop_instance(
    target: str,
    instance_id: str,
    *,
    token: str | None = None,
) -> pb.TaskChangeResponse:
    """迁移期适配：v1 ``instance_id="{task}:{device}"`` → v2 设备级 StopTask。"""
    return await stop_task(target, device_of_instance(instance_id), token=token)


async def get_task(target: str, device_id: str, *, timeout: float = 5.0) -> pb.TaskStatus:
    """查询单设备 Task 状态（GetTask，键为 device_id）。"""
    async with _channel(target) as channel:
        return await pb_grpc.CollectorTaskServiceStub(channel).GetTask(
            pb.DeviceIdRequest(device_id=device_id), timeout=timeout
        )


async def get_task_instance(
    target: str, instance_id: str, *, timeout: float = 5.0
) -> pb.TaskStatus:
    """迁移期适配：按 v1 instance_id 查询，等价 :func:`get_task`。"""
    return await get_task(target, device_of_instance(instance_id), timeout=timeout)


async def list_tasks(
    target: str,
    *,
    device_ids: tuple[str, ...] = (),
    timeout: float = 5.0,
) -> list[pb.TaskStatus]:
    """列出全部（或指定设备的）Task 状态，自动翻页。"""
    async with _channel(target) as channel:
        stub = pb_grpc.CollectorTaskServiceStub(channel)
        tasks: list[pb.TaskStatus] = []
        page_token = ""
        while True:
            page = await stub.ListTasks(
                pb.ListTasksRequest(device_ids=list(device_ids), page_token=page_token),
                timeout=timeout,
            )
            tasks.extend(page.tasks)
            if not page.next_page_token:
                return tasks
            page_token = page.next_page_token


async def instance_states(target: str) -> dict[str, str] | None:
    """设备 → 实际状态名（RUNNING/STOPPED）；gRPC 暂不可用时返回 None。

    键为 ``{task_id}:{device_id}`` 形式（v2 TaskStatus 同时带两者），
    保持与恢复测试既有断言习惯一致。
    """
    try:
        tasks = await list_tasks(target)
    except grpc.aio.AioRpcError:
        return None
    return {
        f"{task.task_id}:{task.device_id}": _TASK_ACTUAL_NAMES.get(task.actual_state, "UNKNOWN")
        for task in tasks
    }


async def validate_config(
    target: str,
    yaml_content: str,
    *,
    selector: pb.ConfigSelector | None = None,
    expected_revision: str | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> pb.ValidateConfigResponse:
    """ValidateConfig（不落盘）；默认校验 INSTANCE devices 文档。

    不传 expected_revision 时跳过乐观版本检查（该字段为 optional，空串
    会被服务端当作显式陈旧版本拒绝）。
    """
    request = pb.ValidateConfigRequest(
        selector=selector or _devices_selector(),
        yaml_content=yaml_content,
    )
    if expected_revision is not None:
        request.expected_revision = expected_revision
    async with _channel(target) as channel:
        return await pb_grpc.CollectorConfigServiceStub(channel).ValidateConfig(
            request,
            metadata=_metadata(token),
            timeout=timeout,
        )


async def replace_config(
    target: str,
    yaml_content: str,
    *,
    selector: pb.ConfigSelector | None = None,
    expected_revision: str | None = None,
    request_id: str | None = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> pb.ConfigMutationResponse:
    """ReplaceConfig 整文档替换（原子落盘 + 运行时收敛）。"""
    revision = expected_revision or await config_revision(target, token=token)
    async with _channel(target) as channel:
        return await pb_grpc.CollectorConfigServiceStub(channel).ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=selector or _devices_selector(),
                expected_revision=revision,
                request_id=request_id or f"replace-{uuid.uuid4().hex[:12]}",
                yaml_content=yaml_content,
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )


async def replace_template(
    target: str,
    category: str,
    template_id: str,
    version: str,
    document: dict[str, Any],
    *,
    create: bool = True,
    request_id: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
) -> pb.ConfigMutationResponse:
    """发布一个不可变版本化模板（``templates/<category>/<id>/<ver>.yaml``）。

    模板不可变：新版本发布必须携带 ``expected_revision="CREATE"``（生产
    事务层对不存在文件的强制语义）；变更点表/型号/任务定义一律发布新版本，
    再通过 :func:`replace_config` 把 devices 文档的引用切到新版本。
    """
    revision = "CREATE" if create else await config_revision(target, token=token)
    async with _channel(target) as channel:
        return await pb_grpc.CollectorConfigServiceStub(channel).ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=_template_selector(category, template_id, version),
                expected_revision=revision,
                request_id=request_id or f"replace-template-{uuid.uuid4().hex[:12]}",
                yaml_content=yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )


async def replace_catalog(
    target: str,
    name: str,
    document: dict[str, Any],
    *,
    request_id: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
) -> pb.ConfigMutationResponse:
    """替换无版本目录模板（``templates/units.yaml`` / ``business_points.yaml``）。"""
    selector = pb.ConfigSelector(scope=pb.TEMPLATE, name=name)
    async with _channel(target) as channel:
        current = await pb_grpc.CollectorConfigServiceStub(channel).GetConfig(
            pb.GetConfigRequest(selector=selector),
            metadata=_metadata(token),
            timeout=5.0,
        )
        revision = current.revision
    async with _channel(target) as channel:
        return await pb_grpc.CollectorConfigServiceStub(channel).ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=selector,
                expected_revision=revision,
                request_id=request_id or f"replace-catalog-{uuid.uuid4().hex[:12]}",
                yaml_content=yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )


async def prepare_config(*args: Any, **kwargs: Any) -> pb.ValidateConfigResponse:
    """已迁移：v1 PrepareConfig 的「不落盘校验」语义由 ValidateConfig 承担。"""
    raise AttributeError(
        "prepare_config 已迁移为 validate_config(target, yaml_content, ...)；"
        "v1 config_dir/revision_id 两段事务已移除"
    )


async def reload_config(*args: Any, **kwargs: Any) -> pb.ConfigMutationResponse:
    """已迁移：v1 Prepare/Activate 热重载由 ReplaceConfig 承担。"""
    raise AttributeError(
        "reload_config 已迁移为 replace_config(target, yaml_content, ...)；"
        "v1 config_dir/revision_id 两段事务已移除"
    )


async def metrics(
    target: str,
    *,
    device_ids: tuple[str, ...] = (),
    metric_names: tuple[str, ...] = (),
    window_seconds: int = 0,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """累计计数与每设备计数的 dict 快照（GetMetrics，自动翻页）。"""
    async with _channel(target) as channel:
        stub = pb_grpc.CollectorMetricsServiceStub(channel)
        counters: dict[str, float] = {}
        device_connect_failures: dict[str, float] = {}
        device_reconnects: dict[str, float] = {}
        page_token = ""
        while True:
            response = await stub.GetMetrics(
                pb.GetMetricsRequest(
                    device_ids=list(device_ids),
                    metric_names=list(metric_names),
                    window_seconds=window_seconds,
                    page_token=page_token,
                ),
                timeout=timeout,
            )
            for row in response.metrics:
                if row.device_id:
                    bucket = (
                        device_reconnects
                        if row.name == "device_reconnects"
                        else device_connect_failures
                    )
                    bucket[row.device_id] = row.value
                else:
                    counters[row.name] = row.value
            if not response.next_page_token:
                break
            page_token = response.next_page_token
    return {
        "counters": counters,
        "device_connect_failures": device_connect_failures,
        "device_reconnects": device_reconnects,
    }


async def metric_events(
    target: str,
    *,
    device_ids: tuple[str, ...] = (),
    event_types: tuple[str, ...] = (),
    limit: int = 1000,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """有界诊断事件（GetMetricsEvents），按时间顺序返回 dict 列表。"""
    from google.protobuf import timestamp_pb2

    async with _channel(target) as channel:
        response = await pb_grpc.CollectorMetricsServiceStub(channel).GetMetricsEvents(
            pb.GetMetricsEventsRequest(
                device_ids=list(device_ids),
                event_types=list(event_types),
                limit=limit,
            ),
            timeout=timeout,
        )
    events: list[dict[str, Any]] = []
    for item in response.events:
        ts = timestamp_pb2.Timestamp()
        ts.CopyFrom(item.timestamp)
        events.append(
            {
                "timestamp": ts.ToJsonString(),
                "kind": item.event_type,
                "object": item.device_id,
                "message": item.message,
                "severity": item.severity,
            }
        )
    return events


async def metrics_snapshot(target: str, **kwargs: Any) -> dict[str, Any]:
    """v1 习惯的完整快照：累计/每设备计数 + 诊断事件。"""
    snapshot = await metrics(target, **kwargs)
    snapshot["events"] = await metric_events(target)
    return snapshot
