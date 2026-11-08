"""System / Reliability 层的 Collector v1 控制面 RPC helper。

system 测试需要驱动 Task 生命周期时，直接以 gRPC 调用 Collector 的两个
现行服务——与 CLI / Web Server → Collector 的生产控制路径一致，不经过
任何中转，也不使用任何兼容代理：

- 身份与健康经 CommonService（GetServiceInfo/GetServiceStatus）；
- 生命周期以 **设备** 为粒度（CollectorService StartTask/StopTask），
  OperationRequest 携带 GetTask 返回的当前 task.revision 作为乐观栅栏；
- START/STOP 意图由 Collector 持久化，冷启动按意图恢复；
- 配置只有快照事务（GetConfigSnapshot/PushConfigSnapshot/ApplyConfigSnapshot），
  单文档校验/替换在本 helper 内以「取整盘快照 → 替换一个文件 → Push（不落盘）
  /Push+Apply（落盘）」实现，不复活任何单文档 RPC；
- 指标为 service 作用域累计计数（CommonService GetMetrics），诊断事件经
  GetEvents（v1 Metric 不含 device_id，每设备下钻改为空字典，由事件补充）。

进程必须以同一管理员令牌启动（WIND_HUB_COLLECTOR_ADMIN_TOKEN）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import grpc
import yaml

from contracts import collector_pb2 as collector_pb
from contracts import collector_pb2_grpc as collector_grpc
from contracts import common_pb2 as common_pb
from contracts import common_pb2_grpc as common_grpc

#: system harness 启动 Collector 时统一注入的测试管理员令牌。
DEFAULT_ADMIN_TOKEN = "system-test-admin-token"


def _admin_token(token: str | None) -> str:
    return token or os.environ.get("WIND_HUB_COLLECTOR_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)


def _metadata(token: str | None) -> tuple[tuple[str, str], ...]:
    return (("x-windhub-admin-token", _admin_token(token)),)


def _channel(target: str) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(target)


def device_of_instance(instance_id: str) -> str:
    """``{task_id}:{device_id}`` → 设备粒度 ID（无冒号时原样返回）。"""
    return instance_id.rpartition(":")[2] or instance_id


@dataclass
class ConfigCheckResult:
    """PushConfigSnapshot 的校验视图（不落盘），兼容旧 ``.valid/.errors`` 断言。"""

    valid: bool
    errors: list[Any] = field(default_factory=list)


@dataclass
class ConfigMutationResult:
    """Push+Apply 快照事务结果，兼容旧 ``.success/.errors/.stored_revision`` 断言。"""

    success: bool
    stored_revision: str
    errors: list[Any] = field(default_factory=list)


def _selector_path(selector: common_pb.SnapshotFile | Any) -> str:
    """v2 风格 ConfigSelector → v1 快照树内的 relative_path。"""
    scope = getattr(selector, "scope", None)
    name = selector.name
    if scope == collector_selector_instance():
        return f"instances/{name}.yaml"
    if scope == collector_selector_template():
        # v1 版本化模板统一扁平命名："category/id" + version →
        # templates/category/id_version.yaml；无版本的目录模板
        # （units/business_points）→ templates/<name>.yaml。
        version = getattr(selector, "version", "")
        if version:
            return f"templates/{name}_{version}.yaml"
        return f"templates/{name}.yaml"
    raise ValueError(f"unsupported selector: {selector}")


def collector_selector_instance() -> int:
    """本地常量，避免在模块顶层引用已退役的 v2 枚举值。"""
    return 1


def collector_selector_template() -> int:
    return 2


async def _disk_snapshot(
    channel: grpc.aio.Channel, token: str | None
) -> common_pb.GetConfigSnapshotResponse:
    stub = common_grpc.CommonServiceStub(channel)
    return await stub.GetConfigSnapshot(
        common_pb.GetConfigSnapshotRequest(scope=common_pb.SNAPSHOT_SCOPE_DISK),
        metadata=_metadata(token),
        timeout=10.0,
    )


async def _swap_one_file(
    target: str,
    relative_path: str,
    yaml_content: str,
    *,
    token: str | None,
    expected_revision: str | None,
) -> tuple[common_pb.PushConfigSnapshotResponse, str]:
    """取整盘快照并替换单个文档，返回 Push 响应与基准 revision。"""
    async with _channel(target) as channel:
        current = await _disk_snapshot(channel, token)
        revision = expected_revision or current.revision
        files: dict[str, bytes] = {
            item.relative_path: bytes(item.content) for item in current.files
        }
        if relative_path not in files:
            raise FileNotFoundError(f"{relative_path} not present in DISK snapshot")
        files[relative_path] = yaml_content.encode("utf-8")
        pushed = await common_grpc.CommonServiceStub(channel).PushConfigSnapshot(
            common_pb.PushConfigSnapshotRequest(
                files=[
                    common_pb.SnapshotFile(
                        relative_path=path,
                        content=data,
                        size_bytes=len(data),
                    )
                    for path, data in sorted(files.items())
                ],
                expected_revision=revision,
            ),
            metadata=_metadata(token),
            timeout=20.0,
        )
    return pushed, revision


async def collector_info(target: str, *, timeout: float = 5.0) -> common_pb.GetServiceInfoResponse:
    """Collector 身份与合同信息（CommonService GetServiceInfo）。"""
    async with _channel(target) as channel:
        return await common_grpc.CommonServiceStub(channel).GetServiceInfo(
            common_pb.Empty(), timeout=timeout
        )


async def runtime_status(target: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """聚合服务健康/设备/任务/发布通道的 runtime dict 快照。"""
    async with _channel(target) as channel:
        common_stub = common_grpc.CommonServiceStub(channel)
        device_stub = collector_grpc.CollectorServiceStub(channel)
        service = await common_stub.GetServiceStatus(common_pb.Empty(), timeout=timeout)
        devices = await _list_all(
            device_stub.ListDevices, collector_pb.ListDevicesRequest, "devices", timeout
        )
        tasks = await _list_all(
            device_stub.ListTasks, collector_pb.ListTasksRequest, "tasks", timeout
        )
        publishing = await device_stub.GetPublishingStatus(
            collector_pb.GetPublishingStatusRequest(), timeout=timeout
        )
    connected = sum(1 for device in devices if device.actual_state == "RUNNING")
    running = sum(1 for task in tasks if task.actual_state == "RUNNING")
    nats_connected = any(channel_status.nats_connected for channel_status in publishing.channels)
    return {
        "running": service.health in {"HEALTHY", "DEGRADED"},
        "health": service.health,
        "device_total": len(devices),
        "device_connected": connected,
        "device_disconnected": len(devices) - connected,
        "task_total": len(tasks),
        "task_running": running,
        "task_stopped": len(tasks) - running,
        "nats_connected": nats_connected,
        "config_status": "APPLIED",
    }


async def runtime_status_snapshot(target: str) -> dict[str, Any] | None:
    """runtime 状态 dict；gRPC 暂不可用时返回 None（供轮询）。"""
    try:
        return await runtime_status(target)
    except grpc.aio.AioRpcError:
        return None


async def config_revision(target: str, *, token: str | None = None, timeout: float = 5.0) -> str:
    """DISK 快照当前 revision（START/STOP/配置变更的乐观版本）。"""
    async with _channel(target) as channel:
        response = await _disk_snapshot(channel, token)
        return str(response.revision)


async def disk_snapshot_files(target: str, *, token: str | None = None) -> dict[str, bytes]:
    """DISK 快照的 ``{relative_path: content}``——进程边界的持久化事实来源。

    v1 配置代际不可变且存放在 state-dir/generations 下，源配置树在启动采集后
    不再被回写；意图落盘断言必须经本 helper 读取已提交快照，而非读源文件。
    """
    async with _channel(target) as channel:
        response = await _disk_snapshot(channel, token)
    return {item.relative_path: bytes(item.content) for item in response.files}


async def start_task(
    target: str,
    device_id: str,
    *,
    token: str | None = None,
    expected_revision: str | None = None,
    timeout: float = 10.0,
) -> common_pb.OperationResult:
    """设备级 START：携带 task.revision 栅栏与管理员元数据。"""
    async with _channel(target) as channel:
        stub = collector_grpc.CollectorServiceStub(channel)
        current = await stub.GetTask(common_pb.ObjectId(id=device_id), timeout=timeout)
        result = await stub.StartTask(
            common_pb.OperationRequest(
                id=device_id,
                expected_revision=expected_revision or current.task.revision,
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )
        return result


async def stop_task(
    target: str,
    device_id: str,
    *,
    token: str | None = None,
    expected_revision: str | None = None,
    timeout: float = 10.0,
) -> common_pb.OperationResult:
    """设备级 STOP（幂等意图，持久化；手动 STOP 后看门狗不得自动复活）。"""
    async with _channel(target) as channel:
        stub = collector_grpc.CollectorServiceStub(channel)
        current = await stub.GetTask(common_pb.ObjectId(id=device_id), timeout=timeout)
        return await stub.StopTask(
            common_pb.OperationRequest(
                id=device_id,
                expected_revision=expected_revision or current.task.revision,
            ),
            metadata=_metadata(token),
            timeout=timeout,
        )


async def start_task_instance(
    target: str,
    *,
    task_id: str = "",  # v1 设备粒度，task_id 仅保留签名兼容
    instance_id: str,
    placement_generation: int = 1,  # placement 栅栏已移除，参数仅为签名兼容
    token: str | None = None,
) -> common_pb.OperationResult:
    """``instance_id="{task}:{device}"`` → 设备级 StartTask。"""
    return await start_task(target, device_of_instance(instance_id), token=token)


async def stop_instance(
    target: str,
    instance_id: str,
    *,
    token: str | None = None,
) -> common_pb.OperationResult:
    """``instance_id="{task}:{device}"`` → 设备级 StopTask。"""
    return await stop_task(target, device_of_instance(instance_id), token=token)


async def get_task(target: str, device_id: str, *, timeout: float = 5.0) -> collector_pb.Task:
    """查询单设备 Task（GetTask，键为 device_id）。"""
    async with _channel(target) as channel:
        response = await collector_grpc.CollectorServiceStub(channel).GetTask(
            common_pb.ObjectId(id=device_id), timeout=timeout
        )
        return response.task


async def get_task_instance(
    target: str, instance_id: str, *, timeout: float = 5.0
) -> collector_pb.Task:
    """按 ``{task}:{device}`` 实例 ID 查询，等价 :func:`get_task`。"""
    return await get_task(target, device_of_instance(instance_id), timeout=timeout)


async def list_tasks(
    target: str,
    *,
    device_ids: tuple[str, ...] = (),
    timeout: float = 5.0,
) -> list[collector_pb.Task]:
    """列出全部 Task（v1 自动翻页）。

    v1 ListTasksRequest 无 device_ids 过滤字段，传入时在客户端过滤。
    """
    async with _channel(target) as channel:
        tasks = await _list_all(
            collector_grpc.CollectorServiceStub(channel).ListTasks,
            collector_pb.ListTasksRequest,
            "tasks",
            timeout,
        )
    if device_ids:
        wanted = set(device_ids)
        tasks = [task for task in tasks if task.device_id in wanted]
    return tasks


async def instance_states(target: str) -> dict[str, str] | None:
    """``{task_id}:{device_id}`` → 实际状态名；gRPC 暂不可用时返回 None。"""
    try:
        tasks = await list_tasks(target)
    except grpc.aio.AioRpcError:
        return None
    return {f"{task.task_id}:{task.device_id}": task.actual_state for task in tasks}


async def _list_all(call: Any, request_type: Any, repeated_field: str, timeout: float) -> list[Any]:
    """v1 字符串 page_token 翻页。"""
    rows: list[Any] = []
    page_token = ""
    while True:
        page = await call(request_type(page_size=200, page_token=page_token), timeout=timeout)
        rows.extend(getattr(page, repeated_field))
        if not page.next_page_token:
            return rows
        page_token = page.next_page_token


async def validate_config(
    target: str,
    yaml_content: str,
    *,
    selector: Any = None,
    expected_revision: str | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> ConfigCheckResult:
    """快照式校验（不落盘）：替换单文档后只 Push 不 Apply。

    默认校验 ``instances/devices.yaml``。expected_revision 不传时使用磁盘
    当前 revision（Push 的乐观锁字段不可省空串）。
    """
    del timeout  # 超时由 RPC 调用固定值控制
    path = _selector_path(selector) if selector is not None else "instances/devices.yaml"
    pushed, _ = await _swap_one_file(
        target, path, yaml_content, token=token, expected_revision=expected_revision
    )
    return ConfigCheckResult(valid=bool(pushed.valid), errors=list(pushed.validation))


async def _apply_pushed(
    channel: grpc.aio.Channel,
    pushed: common_pb.PushConfigSnapshotResponse,
    revision: str,
) -> common_pb.ApplyConfigSnapshotResponse:
    return await common_grpc.CommonServiceStub(channel).ApplyConfigSnapshot(
        common_pb.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id,
            expected_revision=revision,
        ),
        metadata=(("x-windhub-admin-token", _admin_token(None)),),
        timeout=30.0,
    )


async def replace_config(
    target: str,
    yaml_content: str,
    *,
    selector: Any = None,
    expected_revision: str | None = None,
    request_id: str | None = None,  # v1 快照事务无单请求 ID，仅保留签名兼容
    token: str | None = None,
    timeout: float = 15.0,  # 透传默认 RPC 超时，保留旧 helper 签名
) -> ConfigMutationResult:
    """整文档替换：快照内换一个文件 → Push → Apply（原子落盘 + 运行时收敛）。"""
    path = _selector_path(selector) if selector is not None else "instances/devices.yaml"
    pushed, revision = await _swap_one_file(
        target, path, yaml_content, token=token, expected_revision=expected_revision
    )
    if not pushed.valid:
        return ConfigMutationResult(False, revision, list(pushed.validation))
    async with _channel(target) as channel:
        applied = await _apply_pushed(channel, pushed, revision)
    failures = [
        result
        for result in applied.apply_results
        if result.status not in {"OK", "APPLIED", "RESTART_PENDING"}
    ]
    return ConfigMutationResult(
        success=not failures,
        stored_revision=str(applied.effective_revision or revision),
        errors=failures,
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
) -> ConfigMutationResult:
    """发布不可变版本化模板（快照事务替换 ``templates/<category>/<id>/<ver>.yaml``）。"""
    del create, request_id, timeout  # v1 无 CREATE 哨兵：新文件随整盘快照一并提交
    selector = _TemplateSelector(category, template_id, version)
    return await replace_config(
        target,
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        selector=selector,
        token=token,
    )


async def replace_catalog(
    target: str,
    name: str,
    document: dict[str, Any],
    *,
    request_id: str | None = None,
    token: str | None = None,
    timeout: float = 20.0,
) -> ConfigMutationResult:
    """替换无版本目录模板（``templates/units.yaml`` / ``business_points.yaml``）。"""
    del request_id, timeout
    selector = _CatalogSelector(name)
    return await replace_config(
        target,
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        selector=selector,
        token=token,
    )


@dataclass
class _TemplateSelector:
    category: str
    template_id: str
    version: str
    scope: int = field(default_factory=collector_selector_template)

    @property
    def name(self) -> str:
        return f"{self.category}/{self.template_id}"


@dataclass
class _CatalogSelector:
    file_name: str
    scope: int = field(default_factory=collector_selector_template)

    @property
    def name(self) -> str:
        return self.file_name


async def prepare_config(*args: Any, **kwargs: Any) -> ConfigCheckResult:
    """已退役：v1 无两段事务，「不落盘校验」由 validate_config 承担。"""
    raise AttributeError(
        "prepare_config 已迁移为 validate_config(target, yaml_content, ...)；"
        "v1 config_dir/revision_id 两段事务已移除"
    )


async def reload_config(*args: Any, **kwargs: Any) -> ConfigMutationResult:
    """已退役：热重载由快照事务 replace_config 承担。"""
    raise AttributeError(
        "reload_config 已迁移为 replace_config(target, yaml_content, ...)；"
        "v1 config_dir/revision_id 两段事务已移除"
    )


async def metrics(
    target: str,
    *,
    device_ids: tuple[str, ...] = (),
    metric_names: tuple[str, ...] = (),
    window_seconds: int = 0,  # v1 窗口单位为 window_ms，service 累计口径默认
    timeout: float = 5.0,
) -> dict[str, Any]:
    """service 作用域累计计数 dict（GetMetrics）。

    v1 Metric 消息不携带 device_id 字段，但生产遥测把每设备计数记录为
    ``collector.device.<id>.reconnections`` / ``.disconnections`` 等带前缀
    指标名，service 作用域响应原样包含它们（device 作用域仅做前缀过滤）。
    本 helper 解析前缀重建每设备字典，语义映射：

    - ``device_reconnects`` ← ``collector.device.<id>.reconnections``；
    - ``device_connect_failures`` ← ``collector.device.<id>.disconnections``
      （v1 无独立连接失败计数，每次故障恢复先记一次断连，为最接近的诚实口径）。
    """
    async with _channel(target) as channel:
        response = await common_grpc.CommonServiceStub(channel).GetMetrics(
            common_pb.GetMetricsRequest(scope="service"),
            timeout=timeout,
        )
    counters: dict[str, float] = {}
    reconnects: dict[str, float] = {}
    failures: dict[str, float] = {}
    prefix = "collector.device."
    for row in response.metrics:
        if metric_names and row.name not in metric_names:
            continue
        counters[row.name] = row.value
        if not row.name.startswith(prefix):
            continue
        rest = row.name[len(prefix) :]
        device_id, separator, device_metric = rest.partition(".")
        if not separator or (device_ids and device_id not in device_ids):
            continue
        if device_metric == "reconnections":
            reconnects[device_id] = row.value
        elif device_metric == "disconnections":
            failures[device_id] = row.value
    return {
        "counters": counters,
        "device_connect_failures": failures,
        "device_reconnects": reconnects,
    }


async def metric_events(
    target: str,
    *,
    device_ids: tuple[str, ...] = (),
    event_types: tuple[str, ...] = (),
    limit: int = 1000,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """有界诊断事件（GetEvents 翻页），按时间顺序返回 dict 列表。"""
    events: list[dict[str, Any]] = []
    page_token = ""
    async with _channel(target) as channel:
        stub = common_grpc.CommonServiceStub(channel)
        while len(events) < limit:
            response = await stub.GetEvents(
                common_pb.GetEventsRequest(
                    page_size=min(200, limit - len(events)),
                    page_token=page_token,
                ),
                metadata=_metadata(None),
                timeout=timeout,
            )
            for item in response.events:
                if event_types and not (
                    item.error_code in event_types or item.severity in event_types
                ):
                    continue
                if device_ids and item.source not in device_ids:
                    continue
                events.append(
                    {
                        "timestamp": item.timestamp.ToJsonString(),
                        "kind": item.error_code or item.severity,
                        "object": item.source,
                        "message": item.message,
                        "severity": item.severity,
                    }
                )
            if not response.next_page_token:
                break
            page_token = response.next_page_token
    return events[:limit]


async def metrics_snapshot(target: str, **kwargs: Any) -> dict[str, Any]:
    """完整快照：service 累计计数 + 诊断事件。"""
    snapshot = await metrics(target, **kwargs)
    snapshot["events"] = await metric_events(target)
    return snapshot
