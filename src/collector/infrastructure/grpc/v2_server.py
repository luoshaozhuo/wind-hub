"""Collector v2 unary gRPC control plane: four services, 17 RPCs."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import grpc
import yaml
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict

from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from core.application.errors import ConfigError
from core.infrastructure.config.collector_documents import CollectorDocumentLoader
from core.infrastructure.config.versioned_templates import config_hash
from core.infrastructure.config.yaml import _UniqueKeyLoader, read_yaml_mapping
from core.infrastructure.rpc.scalar_codec import encode_scalar

from ...application.services import CollectorQueryService
from ...assembly import CollectorApp
from ..config.config_patches import ConfigPatch
from ..config.transactions import (
    ConfigMutationResult,
    ConfigSelector,
    PublishedTemplateExistsError,
    RevisionConflictError,
    redact_config,
)


def _json_value(value: Any) -> Any:
    """Restore integral JSON numbers lost by protobuf Value's double encoding."""
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53:
        return int(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _required(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value


async def _bad(context: grpc.aio.ServicerContext, exc: Exception) -> None:
    status = grpc.StatusCode.INVALID_ARGUMENT
    if isinstance(exc, RevisionConflictError):
        status = grpc.StatusCode.ABORTED
    elif isinstance(exc, PublishedTemplateExistsError):
        status = grpc.StatusCode.ALREADY_EXISTS
    elif isinstance(exc, KeyError):
        status = grpc.StatusCode.NOT_FOUND
    elif isinstance(exc, PermissionError):
        status = grpc.StatusCode.PERMISSION_DENIED
    await context.abort(status, str(exc))


def _selector(raw: pb.ConfigSelector) -> ConfigSelector:
    scope = pb.ConfigScope.Name(raw.scope)
    scope = scope.removeprefix("CONFIG_SCOPE_")
    name = _required(raw.name, "selector.name")
    if scope == "TEMPLATE":
        if name in {"units", "business_points"} and not raw.version:
            return ConfigSelector(scope, name)
        pieces = name.split("/")
        if len(pieces) != 2:
            raise ValueError("template selector must be category/id")
        return ConfigSelector(scope, pieces[0], pieces[1], _required(raw.version, "version"))
    return ConfigSelector(scope, name)


async def _admin_only(context: grpc.aio.ServicerContext) -> None:
    expected = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
    if not expected:
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "admin access disabled")
    credentials = dict(context.invocation_metadata())
    token = credentials.get("x-windhub-admin-token", "")
    if not hmac.compare_digest(token, expected or ""):
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "admin access denied")


def _parse_yaml(text: str) -> dict[str, object]:
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("yaml_content requires a mapping")
    return document


def _effective_devices(
    app: CollectorApp,
) -> tuple[dict[str, object], list[pb.TemplateReference]]:
    """Return resolved device instances, with no raw secrets in the response."""
    documents = CollectorDocumentLoader(app.config_paths).load()
    results: dict[str, object] = {}
    refs: dict[tuple[str, str], pb.TemplateReference] = {}
    for device_id, device in documents.devices.items():
        model = device.model.document
        options = dict(model.get("protocol_options", {}))
        options.update(device.overrides.get("protocol_options", {}))
        endpoint = dict(model.get("endpoint_defaults", {}))
        endpoint.update(device.endpoint)
        results[device_id] = {
            "model_id": device.model.reference.template_id,
            "model_version": device.model.reference.template_version,
            "endpoint": endpoint,
            "protocol": device.templates["protocol"].document["protocol"],
            "point_table_id": device.templates["point_table"].reference.template_id,
            "point_table_version": device.templates["point_table"].reference.template_version,
            "task": {
                **{
                    key: value
                    for key, value in device.templates["task"].document.items()
                    if key not in {"schema_version", "template_id", "template_version"}
                },
                **device.overrides.get("task", {}),
                "desired_state": device.desired_state,
            },
            "protocol_options": options,
            "enabled": device.enabled,
        }
        for template in (device.model, *device.templates.values(), *device.inherited_tables):
            category = template.reference.category
            name = f"{category}/{template.reference.template_id}"
            version = template.reference.template_version
            refs[(name, version)] = pb.TemplateReference(name=name, version=version)
    return redact_config({"schema_version": 1, "devices": results}), list(refs.values())


def _affected_devices(
    app: CollectorApp, selector: ConfigSelector, candidate: dict[str, object]
) -> list[str]:
    """Identify impacted devices without changing any persisted configuration."""
    active = {str(device_id) for device_id in app.config.current_config.devices}
    if selector.scope == "SERVICE":
        return sorted(active)
    if selector.scope == "INSTANCE":
        if selector.name == "site":
            return sorted(active)
        current = read_yaml_mapping(app.config_paths.instances / "devices.yaml")
        old_devices = current.get("devices", {})
        new_devices = candidate.get("devices", {})
        if not isinstance(old_devices, dict) or not isinstance(new_devices, dict):
            raise ValueError("devices mapping required")
        return sorted(
            identity
            for identity in set(old_devices) | set(new_devices)
            if old_devices.get(identity) != new_devices.get(identity)
        )
    if selector.name in {"units", "business_points"}:
        return sorted(active)
    documents = CollectorDocumentLoader(app.config_paths).load()
    affected: list[str] = []
    for device_id, definition in documents.devices.items():
        templates = (
            definition.model,
            *definition.templates.values(),
            *definition.inherited_tables,
        )
        if any(
            item.reference.category == selector.name
            and item.reference.template_id == selector.template_id
            and item.reference.template_version == selector.template_version
            for item in templates
        ):
            affected.append(device_id)
    return sorted(affected)


@dataclass(slots=True)
class CollectorGrpcServerV2:
    server: grpc.aio.Server
    endpoint: str

    async def start(self) -> None:
        await self.server.start()

    async def stop(self, grace: float = 5.0) -> None:
        await self.server.stop(grace)


class CollectorRuntimeServiceV2(pb_grpc.CollectorRuntimeServiceServicer):
    """Bounded, memory-only runtime queries."""

    def __init__(self, app: CollectorApp) -> None:
        self._app = app
        self._query = CollectorQueryService(app.runtime)
        self._started_at = datetime.now(UTC)

    async def GetCollectorInfo(
        self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext
    ) -> pb.CollectorInfoResponse:
        del request, context
        response = pb.CollectorInfoResponse(
            collector_id=self._app.identity.collector_id,
            boot_id=self._app.identity.boot_id,
            version="0.1.0",
            uptime_seconds=(datetime.now(UTC) - self._started_at).total_seconds(),
            site_id=self._app.site_id,
            active_revision=self._app.config.active_revision,
        )
        response.started_at.FromDatetime(self._started_at)
        return response

    async def GetRuntimeStatus(
        self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext
    ) -> pb.RuntimeStatusResponse:
        del request, context
        sessions = self._app.runtime.devices
        states = self._app.runtime.instance_states()
        connected = sum(1 for item in sessions.values() if item.is_open())
        running = sum(1 for state in states.values() if state.value == "running")
        return pb.RuntimeStatusResponse(
            running=self._app.runtime.running,
            device_total=len(sessions),
            device_connected=connected,
            device_disconnected=len(sessions) - connected,
            task_total=len(states),
            task_running=running,
            task_stopped=len(states) - running,
            nats_connected=self._app.runtime.nats_connected,
            config_status={
                "APPLIED": pb.APPLIED,
                "RESTART_REQUIRED": pb.RESTART_REQUIRED,
                "APPLY_FAILED": pb.APPLY_FAILED,
            }[self._app.config_admin.runtime_status],
        )

    async def ListDevices(
        self, request: pb.ListDevicesRequest, context: grpc.aio.ServicerContext
    ) -> pb.ListDevicesResponse:
        try:
            size = min(request.page_size or 100, 200)
            offset = int(request.page_token or "0")
            if offset < 0:
                raise ValueError("invalid page token")
        except ValueError as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None
        items = await self._query.list_devices()
        requested = set(request.device_ids)
        if requested:
            items = [row for row in items if row.device_id in requested]
        if request.HasField("protocol"):
            items = [row for row in items if row.protocol == request.protocol]
        if request.HasField("connected"):
            items = [row for row in items if row.connected == request.connected]
        items.sort(key=lambda row: row.device_id)
        page = items[offset : offset + size]
        states = self._app.runtime.instance_states()
        intent = self._app.runtime.desired_states()
        return pb.ListDevicesResponse(
            devices=[
                pb.DeviceStatus(
                    device_id=row.device_id,
                    protocol=row.protocol,
                    connected=row.connected,
                    consecutive_failures=row.consecutive_failures,
                    desired_state=pb.START if intent.get(row.device_id) == "START" else pb.STOP,
                    actual_state=(
                        pb.RUNNING
                        if states.get(row.device_id) is not None
                        and states[row.device_id].value == "running"
                        else pb.STOPPED
                    ),
                )
                for row in page
            ],
            next_page_token=str(offset + size) if offset + size < len(items) else "",
        )

    async def GetDevice(
        self, request: pb.DeviceIdRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetDeviceResponse:
        try:
            device_id = _required(request.device_id, "device_id")
            rows = await self._query.list_devices()
            detail = next(row for row in rows if row.device_id == device_id)
            from core.domain import DeviceId

            current = self._app.config.current_config
            device = current.devices[DeviceId(device_id)]
            model_ref = str(device.device_model_id)
            model_id, _, model_version = model_ref.partition("@")
        except (KeyError, ValueError, StopIteration) as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            raise AssertionError("RPC aborted") from None
        intent = self._app.runtime.desired_states()
        state = self._app.runtime.instance_states().get(device_id)
        return pb.GetDeviceResponse(
            device=pb.DeviceStatus(
                device_id=device_id,
                protocol=detail.protocol,
                connected=detail.connected,
                consecutive_failures=detail.consecutive_failures,
                desired_state=pb.START if intent.get(device_id) == "START" else pb.STOP,
                actual_state=(
                    pb.RUNNING if state is not None and state.value == "running" else pb.STOPPED
                ),
            ),
            model_id=model_id,
            model_version=model_version,
            task_id=device_id,
        )

    async def GetLatestValues(
        self, request: pb.GetLatestValuesRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetLatestValuesResponse:
        try:
            device_id = _required(request.device_id, "device_id")
            if device_id not in self._app.runtime.devices:
                raise KeyError(device_id)
            if len(request.point_ids) > 10000:
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "too many point IDs")
        except (ValueError, KeyError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None
        selected = set(request.point_ids)
        records = {
            item.point_id: item
            for item in self._app.latest_values.snapshot(device_id)
            if not selected or item.point_id in selected
        }
        requested = request.point_ids or sorted(records)
        response = pb.GetLatestValuesResponse()
        now = datetime.now(UTC)
        for point_id in requested:
            item = records.get(point_id)
            row = response.values.add(point_id=point_id, found=item is not None)
            if item is None:
                continue
            row.value.CopyFrom(encode_scalar(item.value))
            row.quality = item.quality.value
            row.sample_time.FromDatetime(item.timestamp)
            if item.receive_time is not None:
                row.receive_time.FromDatetime(item.receive_time)
            row.age_ms = max(0.0, (now - item.timestamp).total_seconds() * 1000)
        return response

    async def GetPointMetadata(
        self, request: pb.GetPointMetadataRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetPointMetadataResponse:
        from contracts import common_pb2
        from core.domain import DeviceId

        try:
            device_id = _required(request.device_id, "device_id")
            config = self._app.config.current_config
            table = config.point_table_for_device(DeviceId(device_id))
        except (ValueError, KeyError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None
        point_ids = request.point_ids or sorted(table.points)
        if len(point_ids) > 10000:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "too many point IDs")
        response = pb.GetPointMetadataResponse(active_revision=self._app.config.active_revision)
        for point_id in point_ids:
            point = table.points.get(point_id)
            if point is None:
                continue
            response.points.add(
                point_id=point.point_id,
                business_point_id=str(point.business_point_id),
                data_type=str(point.ext.get("data_type") or ""),
                unit_id=point.source_unit.unit_id,
                quantity_id=point.source_unit.quantity.quantity_id,
                access=point.access.value,
                description=point.description or "",
                scale=point.scale,
                offset=point.offset,
                protocol_address=[
                    common_pb2.AddressField(key=key, value=encode_scalar(value))
                    for key, value in point.ext.items()
                    if key != "data_type" and value is not None
                ],
            )
        return response


class CollectorTaskServiceV2(pb_grpc.CollectorTaskServiceServicer):
    """Device-scoped Task status and revision-checked START/STOP."""

    def __init__(self, app: CollectorApp) -> None:
        self._app = app

    def _task(self, device_id: str) -> pb.TaskStatus:
        instances = self._app.runtime.task_instances()
        try:
            instance = instances[device_id]
            state = self._app.runtime.instance_states()[device_id]
        except KeyError:
            raise KeyError(f"task for device {device_id!r} not found") from None
        session = self._app.runtime.devices[device_id]
        row = pb.TaskStatus(
            task_id=instance.task_id,
            device_id=device_id,
            template_id="",
            mode=session.acquisition_mode.value.upper(),
            desired_state=(
                pb.START
                if self._app.runtime.desired_states().get(device_id) == "START"
                else pb.STOP
            ),
            actual_state=pb.RUNNING if state.value == "running" else pb.STOPPED,
        )
        if instance.interval is not None:
            row.interval_ms = round(instance.interval * 1000)
        return row

    async def ListTasks(
        self, request: pb.ListTasksRequest, context: grpc.aio.ServicerContext
    ) -> pb.ListTasksResponse:
        try:
            page_size = min(request.page_size or 100, 200)
            offset = int(request.page_token or "0")
            if offset < 0:
                raise ValueError("invalid page token")
        except ValueError as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None
        requested = set(request.device_ids)
        tasks = [
            self._task(device_id)
            for device_id in sorted(self._app.runtime.task_instances())
            if not requested or device_id in requested
        ]
        if request.HasField("desired_state"):
            tasks = [task for task in tasks if task.desired_state == request.desired_state]
        if request.HasField("actual_state"):
            tasks = [task for task in tasks if task.actual_state == request.actual_state]
        return pb.ListTasksResponse(
            tasks=tasks[offset : offset + page_size],
            next_page_token=(str(offset + page_size) if offset + page_size < len(tasks) else ""),
        )

    async def GetTask(
        self, request: pb.DeviceIdRequest, context: grpc.aio.ServicerContext
    ) -> pb.TaskStatus:
        try:
            return self._task(_required(request.device_id, "device_id"))
        except (ValueError, KeyError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None

    async def _change(
        self,
        request: pb.TaskChangeRequest,
        context: grpc.aio.ServicerContext,
        desired: str,
    ) -> pb.TaskChangeResponse:
        await _admin_only(context)
        try:
            device_id = _required(request.device_id, "device_id")
            revision = _required(request.expected_revision, "expected_revision")
            request_id = _required(request.request_id, "request_id")
            result = await self._app.config_admin.set_task_desired(
                device_id,
                desired,
                expected_revision=revision,
                request_id=request_id,
            )
            task = self._task(device_id)
            return pb.TaskChangeResponse(
                device_id=device_id,
                desired_state=pb.START if desired == "START" else pb.STOP,
                actual_state=task.actual_state,
                stored_revision=result.stored_revision,
                converged=result.status == "APPLIED"
                and task.actual_state == (pb.RUNNING if desired == "START" else pb.STOPPED),
                error="runtime not converged" if result.status == "APPLY_FAILED" else "",
            )
        except (ValueError, KeyError, ConfigError, PermissionError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None

    async def StartTask(
        self, request: pb.TaskChangeRequest, context: grpc.aio.ServicerContext
    ) -> pb.TaskChangeResponse:
        return await self._change(request, context, "START")

    async def StopTask(
        self, request: pb.TaskChangeRequest, context: grpc.aio.ServicerContext
    ) -> pb.TaskChangeResponse:
        return await self._change(request, context, "STOP")


class CollectorConfigServiceV2(pb_grpc.CollectorConfigServiceServicer):
    """Typed configuration read/validate/write/status API."""

    def __init__(self, app: CollectorApp) -> None:
        self._app = app

    async def GetConfig(
        self, request: pb.GetConfigRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetConfigResponse:
        await _admin_only(context)
        try:
            selector = _selector(request.selector)
            if request.effective and selector.scope != "INSTANCE":
                raise ValueError("effective view is supported only for instances")
            document, revision = self._app.config_admin.get(selector)
            refs: list[pb.TemplateReference] = []
            digest = revision
            if request.effective and selector.name == "devices":
                document, refs = _effective_devices(self._app)
                digest = config_hash(document)
            return pb.GetConfigResponse(
                selector=request.selector,
                schema_version=document["schema_version"],
                revision=revision,
                content_hash=digest,
                yaml_content=yaml.safe_dump(document, allow_unicode=True),
                template_refs=refs,
            )
        except (ValueError, KeyError, ConfigError, PermissionError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None

    async def ValidateConfig(
        self, request: pb.ValidateConfigRequest, context: grpc.aio.ServicerContext
    ) -> pb.ValidateConfigResponse:
        await _admin_only(context)
        try:
            selector = _selector(request.selector)
            document = _parse_yaml(request.yaml_content)
            if request.HasField("expected_revision"):
                try:
                    _, revision = self._app.config_admin.get(selector)
                except ConfigError:
                    revision = "CREATE"
                if revision != request.expected_revision:
                    raise ConfigError("configuration revision conflict")
            digest = self._app.config_admin.validate(selector, document)
            impacted = _affected_devices(self._app, selector, document)
            restart_required = (
                selector.scope == "SERVICE"
                or (selector.scope == "INSTANCE" and selector.name == "site")
                or (selector.scope == "TEMPLATE" and selector.name in {"units", "business_points"})
            )
            return pb.ValidateConfigResponse(
                valid=True,
                candidate_hash=digest,
                affected_devices=impacted,
                restart_required=restart_required,
            )
        except (ValueError, KeyError, ConfigError, PermissionError) as exc:
            return pb.ValidateConfigResponse(
                valid=False,
                errors=[
                    pb.ConfigIssue(
                        code="CONFIG_INVALID",
                        field_path="",
                        message=str(exc),
                    )
                ],
            )

    async def _mutation_reply(self, result: ConfigMutationResult) -> pb.ConfigMutationResponse:
        return pb.ConfigMutationResponse(
            success=result.status in {"APPLIED", "RESTART_REQUIRED"},
            stored_revision=result.stored_revision,
            status={
                "APPLIED": pb.APPLIED,
                "RESTART_REQUIRED": pb.RESTART_REQUIRED,
                "APPLY_FAILED": pb.APPLY_FAILED,
            }.get(result.status, pb.CONFIG_STATUS_UNSPECIFIED),
            restart_required=result.status == "RESTART_REQUIRED",
            affected_devices=[
                pb.DeviceApplyResult(
                    device_id=row.device_id,
                    target_revision=row.target_revision,
                    applied=row.applied,
                    error=row.error,
                )
                for row in result.devices
            ],
            errors=[
                pb.ConfigIssue(code="APPLY_FAILED", message=message) for message in result.errors
            ],
        )

    async def ReplaceConfig(
        self, request: pb.ReplaceConfigRequest, context: grpc.aio.ServicerContext
    ) -> pb.ConfigMutationResponse:
        await _admin_only(context)
        try:
            selector = _selector(request.selector)
            document = _parse_yaml(request.yaml_content)
            result = await self._app.config_admin.replace(
                selector,
                document,
                expected_revision=_required(request.expected_revision, "expected_revision"),
                request_id=_required(request.request_id, "request_id"),
            )
            return await self._mutation_reply(result)
        except (ValueError, KeyError, ConfigError, PermissionError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None

    async def UpdateConfig(
        self, request: pb.UpdateConfigRequest, context: grpc.aio.ServicerContext
    ) -> pb.ConfigMutationResponse:
        await _admin_only(context)
        try:
            selector = _selector(request.selector)
            updates: list[ConfigPatch] = []
            for patch in request.updates:
                if patch.operation == pb.SET:
                    if not patch.HasField("value"):
                        raise ValueError("SET patch requires value")
                    value = _json_value(MessageToDict(patch.value))
                    updates.append(ConfigPatch(patch.path, "SET", value))
                elif patch.operation == pb.REMOVE:
                    if patch.HasField("value"):
                        raise ValueError("REMOVE patch must not contain value")
                    updates.append(ConfigPatch(patch.path, "REMOVE"))
                else:
                    raise ValueError("unsupported patch operation")
            result = await self._app.config_admin.update(
                selector,
                tuple(updates),
                expected_revision=_required(request.expected_revision, "expected_revision"),
                request_id=_required(request.request_id, "request_id"),
            )
            return await self._mutation_reply(result)
        except (ValueError, KeyError, ConfigError, PermissionError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None

    async def GetConfigStatus(
        self, request: pb.GetConfigStatusRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetConfigStatusResponse:
        await _admin_only(context)
        selectors = list(request.selectors)
        if len(selectors) > 100:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "too many selectors")
        if not selectors:
            selectors = [
                pb.ConfigSelector(
                    scope={
                        "SERVICE": pb.SERVICE,
                        "INSTANCE": pb.INSTANCE,
                        "TEMPLATE": pb.TEMPLATE,
                    }[item.scope],
                    name=(f"{item.name}/{item.template_id}" if item.template_id else item.name),
                    version=item.template_version,
                )
                for item in self._app.config_admin.list_selectors()
            ]
        if len(selectors) > 1000:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "too many configuration objects",
            )
        responses: list[pb.ConfigApplyStatus] = []
        try:
            for raw in selectors:
                selector = _selector(raw)
                result = self._app.config_admin.status(selector)
                status = {
                    "APPLIED": pb.APPLIED,
                    "RESTART_REQUIRED": pb.RESTART_REQUIRED,
                    "APPLY_FAILED": pb.APPLY_FAILED,
                }.get(result.status, pb.CONFIG_STATUS_UNSPECIFIED)
                responses.append(
                    pb.ConfigApplyStatus(
                        selector=raw,
                        stored_revision=result.stored_revision,
                        active_revision=result.active_revision,
                        stored_hash=result.stored_revision,
                        active_hash=result.active_revision,
                        status=status,
                        restart_required=status == pb.RESTART_REQUIRED,
                        devices=[
                            pb.DeviceApplyResult(
                                device_id=row.device_id,
                                target_revision=row.target_revision,
                                applied=row.applied,
                                error=row.error,
                            )
                            for row in result.devices
                        ]
                        if request.include_devices
                        else [],
                        errors=[
                            pb.ConfigIssue(code="APPLY_FAILED", message=message)
                            for message in result.errors
                        ],
                    )
                )
        except (ValueError, KeyError, ConfigError, PermissionError) as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None
        aggregate = pb.APPLIED
        if any(row.status == pb.APPLY_FAILED for row in responses):
            aggregate = pb.APPLY_FAILED
        elif any(row.status == pb.RESTART_REQUIRED for row in responses):
            aggregate = pb.RESTART_REQUIRED
        return pb.GetConfigStatusResponse(status=aggregate, configs=responses)


class CollectorMetricsServiceV2(pb_grpc.CollectorMetricsServiceServicer):
    """Memory-only cumulative metrics and bounded diagnostic event queries."""

    def __init__(self, app: CollectorApp) -> None:
        self._app = app

    async def GetMetrics(
        self, request: pb.GetMetricsRequest, context: grpc.aio.ServicerContext
    ) -> pb.GetMetricsResponse:
        try:
            page_size = min(request.page_size or 100, 200)
            offset = int(request.page_token or "0")
            if offset < 0:
                raise ValueError("invalid page token")
        except ValueError as exc:
            await _bad(context, exc)
            raise AssertionError("RPC aborted") from None
        snapshot = self._app.metrics.snapshot()
        names = set(request.metric_names)
        if request.window_seconds:
            try:
                snapshot["counters"] = self._app.metrics.window_snapshot(request.window_seconds)
            except ValueError as exc:
                await _bad(context, exc)
                raise AssertionError("RPC aborted") from None
        rows: list[pb.MetricValue] = []
        snapshot["counters"].update(
            {
                "points_routed": self._app.runtime.points_routed,
                "points_dropped": self._app.runtime.points_dropped,
            }
        )
        for name, value in sorted(snapshot["counters"].items()):
            if names and name not in names:
                continue
            rows.append(
                pb.MetricValue(
                    name=name,
                    value=float(value),
                    unit="count",
                    valid=True,
                    sample_count=1,
                )
            )
        for source, prefix in (
            ("device_connect_failures", "device_connect_failures"),
            ("device_reconnects", "device_reconnects"),
        ):
            for device_id, value in sorted(snapshot[source].items()):
                if request.device_ids and device_id not in request.device_ids:
                    continue
                if names and prefix not in names:
                    continue
                rows.append(
                    pb.MetricValue(
                        name=prefix,
                        device_id=device_id,
                        value=float(value),
                        unit="count",
                        valid=True,
                        sample_count=1,
                    )
                )
        response = pb.GetMetricsResponse(
            collector_id=self._app.identity.collector_id,
            boot_id=self._app.identity.boot_id,
            metrics=rows[offset : offset + page_size],
            next_page_token=(str(offset + page_size) if offset + page_size < len(rows) else ""),
        )
        response.generated_at.FromDatetime(datetime.now(UTC))
        return response

    async def GetMetricsEvents(
        self,
        request: pb.GetMetricsEventsRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.GetMetricsEventsResponse:
        limit = min(request.limit or 100, 1000)
        events = self._app.metrics.snapshot()["events"]
        selected_devices = set(request.device_ids)
        selected_kinds = set(request.event_types)
        since = request.since.ToDatetime(tzinfo=UTC) if request.HasField("since") else None
        filtered: list[tuple[int, dict[str, Any]]] = []
        for index, event in enumerate(events):
            if selected_devices and event["object"] not in selected_devices:
                continue
            if selected_kinds and event["kind"] not in selected_kinds:
                continue
            timestamp = datetime.fromisoformat(event["timestamp"])
            if since is not None and timestamp < since:
                continue
            filtered.append((index, event))
        response = pb.GetMetricsEventsResponse(truncated=len(filtered) > limit)
        for index, event in filtered[:limit]:
            row = response.events.add(
                event_id=f"{self._app.identity.boot_id}:{index}",
                event_type=event["kind"],
                device_id=event["object"],
                severity="WARNING",
                message=event["message"],
            )
            row.timestamp.FromDatetime(datetime.fromisoformat(event["timestamp"]))
        return response


def build_v2_grpc_server(
    app: CollectorApp,
    *,
    host: str,
    port: int,
) -> CollectorGrpcServerV2:
    """Register all v2 RPCs on a loopback-only plaintext listener."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("remote Collector gRPC requires a TLS-authenticated listener")
    server = grpc.aio.server()
    pb_grpc.add_CollectorRuntimeServiceServicer_to_server(
        CollectorRuntimeServiceV2(app),
        server,
    )
    pb_grpc.add_CollectorTaskServiceServicer_to_server(
        CollectorTaskServiceV2(app),
        server,
    )
    pb_grpc.add_CollectorConfigServiceServicer_to_server(
        CollectorConfigServiceV2(app),
        server,
    )
    pb_grpc.add_CollectorMetricsServiceServicer_to_server(
        CollectorMetricsServiceV2(app),
        server,
    )
    bound = server.add_insecure_port(f"{host}:{port}")
    if not bound:
        raise RuntimeError("failed to bind Collector v2 gRPC server")
    return CollectorGrpcServerV2(server, f"{host}:{bound}")
