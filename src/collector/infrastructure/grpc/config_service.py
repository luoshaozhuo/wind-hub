"""Collector's configuration portion of the shared CommonService contract."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import grpc
import yaml

from collector.application.acquisition import CollectorAcquisition
from collector.application.configuration import CollectorConfigurationManager
from collector.application.lifecycle import CollectorLifecycle, Lifecycle
from collector.application.telemetry import CollectorTelemetry, RuntimeEvent
from collector.infrastructure.config.snapshot_repository import SnapshotConflictError, SnapshotError
from collector.infrastructure.publisher import CollectorPublisher
from contracts import common_pb2 as pb
from contracts import common_pb2_grpc as rpc
from core.application.errors import ConfigError

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
        "access_key",
        "api_key",
        "credential",
        "credentials",
    }
)


def _redact_yaml(content: bytes) -> bytes:
    """Redact nested secret values before returning a configuration document."""
    document = yaml.safe_load(content)
    changed = False

    def scrub(value: object) -> object:
        nonlocal changed
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, item in value.items():
                name = str(key)
                sensitive = name.lower() in _SENSITIVE_KEYS or any(
                    marker in name.lower()
                    for marker in ("_password", "_secret", "_token", "_credential")
                )
                if sensitive:
                    # An empty/unset secret carries no value to protect; leave
                    # it untouched so documents that only declare an empty
                    # placeholder stay byte-identical on a DISK round-trip.
                    if item is None or item == "":
                        result[name] = item
                    else:
                        changed = True
                        result[name] = "***REDACTED***"
                else:
                    result[name] = scrub(item)
            return result
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    result = scrub(document)
    if not changed:
        return content
    return yaml.safe_dump(result, allow_unicode=True, sort_keys=False).encode("utf-8")


class CollectorConfigRpc(rpc.CommonServiceServicer):
    """CommonService handlers for Collector, using authoritative Proto types."""

    def __init__(
        self,
        manager: CollectorConfigurationManager,
        lifecycle: CollectorLifecycle,
        acquisition: CollectorAcquisition,
        telemetry: CollectorTelemetry,
        publisher: CollectorPublisher,
    ) -> None:
        self._manager = manager
        self._lifecycle = lifecycle
        self._acquisition = acquisition
        self._telemetry = telemetry
        self._publisher = publisher
        self._started_at = datetime.now(UTC)
        self._event_pages: dict[
            str, tuple[float, tuple[RuntimeEvent, ...], tuple[str, str, str]]
        ] = {}

    async def GetServiceInfo(  # noqa: N802 — method name is required by common.proto
        self, request: pb.Empty, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetServiceInfoResponse:
        del request, context
        response = pb.GetServiceInfoResponse(
            service_id=self._publisher.collector_id,
            process_role="collector",
            software_version="unknown",
            contract_version="windhub.common.v1",
        )
        response.started_at.FromDatetime(self._started_at)
        return response

    async def GetServiceStatus(  # noqa: N802 — method name is required by common.proto
        self, request: pb.Empty, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetServiceStatusResponse:
        del request, context
        state = self._lifecycle.state
        workers = self._acquisition.workers
        faulty = [
            name
            for name, worker in workers.items()
            if (worker.desired_state == "START" and worker.actual_state != "RUNNING")
            or worker.actual_state in {"FAULTED", "ISOLATED", "BLOCKED"}
        ]
        faulty.extend(self._acquisition.errors)
        connection = getattr(self._publisher, "_connection", None)
        nats_healthy = bool(connection and connection.is_connected)
        response = pb.GetServiceStatusResponse(
            health=("DEGRADED" if faulty or not nats_healthy else "HEALTHY")
            if state is Lifecycle.RUNNING
            else state.value,
            dependencies=[
                pb.DependencyHealth(
                    name="acquisition",
                    health="DEGRADED" if faulty else "HEALTHY",
                    detail=",".join(sorted(faulty)),
                ),
                pb.DependencyHealth(
                    name="nats",
                    health="HEALTHY" if nats_healthy else "DEGRADED",
                ),
            ],
        )
        response.observed_at.FromDatetime(datetime.now(UTC))
        return response

    async def GetCapabilities(  # noqa: N802 — method name is required by common.proto
        self, request: pb.Empty, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetCapabilitiesResponse:
        del request, context
        return pb.GetCapabilitiesResponse(
            rpc_names=[
                "GetServiceInfo",
                "GetServiceStatus",
                "GetCapabilities",
                "GetConfigSnapshot",
                "PushConfigSnapshot",
                "ApplyConfigSnapshot",
                "GetMetrics",
                "GetEvents",
                "Diagnose",
                "ListDevices",
                "GetDevice",
                "ListPoints",
                "GetPoint",
                "ReadPoints",
                "ListTasks",
                "GetTask",
                "StartTask",
                "StopTask",
                "GetPublishingStatus",
                "InterrogateDevice",
            ],
            protocols=["ads", "modbus", "iec104"],
            features=["configuration-snapshots", "acquisition", "diagnose", "metrics"],
        )

    @staticmethod
    async def _authorize(context: grpc.aio.ServicerContext[Any, Any]) -> None:
        expected = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
        if not expected:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "configuration access disabled")
        supplied = dict(context.invocation_metadata()).get("x-windhub-admin-token", "")
        # metadata 值类型为 str | bytes；二进制令牌不可能匹配 str 配置，
        # 显式归入拒绝路径，保证 compare_digest 两侧恒为 str（恒定时间比较）。
        if not isinstance(supplied, str) or not hmac.compare_digest(expected or "", supplied):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "configuration access denied")

    def _read_candidate(self, candidate_id: str) -> tuple[str, dict[str, bytes]]:
        return (
            self._manager.candidate_revision(candidate_id),
            self._manager.get_candidate(candidate_id),
        )

    async def GetConfigSnapshot(  # noqa: N802 — method name is required by common.proto
        self, request: pb.GetConfigSnapshotRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetConfigSnapshotResponse:
        await self._authorize(context)
        try:
            if request.scope == pb.SNAPSHOT_SCOPE_DISK:
                # A full generation read verifies a sha256 over up to 16 MiB;
                # keep that disk work off the event loop.
                revision, files = await asyncio.to_thread(self._manager.get_disk)
            elif request.scope == pb.SNAPSHOT_SCOPE_RUNTIME:
                status = self._manager.status()
                revision = status.runtime_revision
                if not revision:
                    raise SnapshotError("no active runtime snapshot")
                files = self._manager.get_runtime()
            elif request.scope == pb.SNAPSHOT_SCOPE_CANDIDATE:
                # A candidate recovered after restart is re-read and re-hashed
                # from disk on first access.
                revision, files = await asyncio.to_thread(
                    self._read_candidate, request.candidate_id
                )
            else:
                raise SnapshotError("snapshot scope is required")
            response = pb.GetConfigSnapshotResponse(
                snapshot_id=(
                    request.candidate_id
                    if request.scope == pb.SNAPSHOT_SCOPE_CANDIDATE
                    else revision
                ),
                revision=revision,
                files=[
                    pb.SnapshotFile(
                        relative_path=path,
                        content=safe_content,
                        size_bytes=len(safe_content),
                        sha256=hashlib.sha256(safe_content).hexdigest(),
                    )
                    for path, content in sorted(files.items())
                    for safe_content in (_redact_yaml(content),)
                ],
            )
            if request.scope != pb.SNAPSHOT_SCOPE_CANDIDATE:
                status = self._manager.status()
                if status.restart_required:
                    response.apply_results.add(
                        object_id="services/collector.yaml",
                        status="RESTART_REQUIRED",
                    )
                for name, message in sorted(status.apply_errors.items()):
                    response.apply_results.add(
                        object_id=name,
                        status="APPLY_FAILED",
                        error_code="CONVERGENCE_FAILED",
                        message=message,
                    )
                if status.apply_errors:
                    response.last_error = "; ".join(status.apply_errors.values())
                if status.disk_revision != status.runtime_revision or status.apply_errors:
                    target = self._manager.target_revision
                    for device_id, worker in sorted(self._acquisition.workers.items()):
                        if device_id in status.apply_errors:
                            continue
                        applied = bool(target and worker.applied_revision == target)
                        response.apply_results.add(
                            object_id=device_id,
                            status="APPLIED" if applied else "PENDING",
                            message=(
                                f"device revision: {worker.applied_revision}"
                                if worker.applied_revision
                                else "device configuration not yet applied"
                            ),
                        )
            return response
        except SnapshotError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("aborted") from exc

    async def PushConfigSnapshot(  # noqa: N802 — method name is required by common.proto
        self, request: pb.PushConfigSnapshotRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.PushConfigSnapshotResponse:
        await self._authorize(context)
        try:
            files: dict[str, bytes] = {}
            for entry in request.files:
                if entry.relative_path in files:
                    raise SnapshotError("duplicate snapshot file")
                if entry.size_bytes != len(entry.content):
                    raise SnapshotError("snapshot size does not match content")
                if entry.sha256 and entry.sha256 != hashlib.sha256(entry.content).hexdigest():
                    raise SnapshotError("snapshot checksum mismatch")
                files[entry.relative_path] = bytes(entry.content)
            candidate_id, base_revision = await asyncio.to_thread(
                self._manager.push,
                files,
                expected_revision=request.expected_revision,
            )
            return pb.PushConfigSnapshotResponse(
                candidate_id=candidate_id,
                valid=True,
                base_revision=base_revision,
            )
        except SnapshotConflictError as exc:
            await context.abort(grpc.StatusCode.ABORTED, str(exc))
        except (SnapshotError, ConfigError, ValueError) as exc:
            return pb.PushConfigSnapshotResponse(
                valid=False,
                validation=[pb.ValidationIssue(path="", code="INVALID_CONFIG", message=str(exc))],
            )
        raise AssertionError("aborted")

    async def ApplyConfigSnapshot(  # noqa: N802 — method name is required by common.proto
        self, request: pb.ApplyConfigSnapshotRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.ApplyConfigSnapshotResponse:
        await self._authorize(context)
        try:
            status = await self._manager.apply(
                request.candidate_id, expected_revision=request.expected_revision
            )
            return pb.ApplyConfigSnapshotResponse(
                file_commit=status.disk_revision,
                effective_revision=status.runtime_revision,
                restart_required=status.restart_required,
                application_id=request.candidate_id,
                apply_results=(
                    [
                        pb.ApplicationResult(
                            object_id=name,
                            status="APPLY_FAILED",
                            error_code="CONVERGENCE_FAILED",
                            message=message,
                        )
                        for name, message in sorted(status.apply_errors.items())
                    ]
                    + (
                        [
                            pb.ApplicationResult(
                                object_id="services/collector.yaml",
                                status="RESTART_REQUIRED",
                            )
                        ]
                        if status.restart_required
                        else []
                    )
                    + (
                        [pb.ApplicationResult(object_id="collector-config", status="APPLIED")]
                        if status.converged
                        else []
                    )
                ),
            )
        except SnapshotConflictError as exc:
            await context.abort(grpc.StatusCode.ABORTED, str(exc))
        except ConfigError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except SnapshotError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        raise AssertionError("aborted")

    async def GetMetrics(  # noqa: N802
        self, request: pb.GetMetricsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetMetricsResponse:
        scope = request.scope
        device_scope = ""
        if scope.startswith("device:") or scope.startswith("task:"):
            device_scope = scope.split(":", 1)[1]
            if not device_scope or device_scope not in self._acquisition.workers:
                await context.abort(grpc.StatusCode.NOT_FOUND, "unknown device/task")
        elif scope not in ("", "service", "collector"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "metrics scope must be service, device:<id>, or task:<id>",
            )
        if request.window_ms:
            if request.window_ms % 1000:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "metrics window must be whole seconds",
                )
            try:
                values = self._telemetry.window_metrics(request.window_ms // 1000)
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        else:
            values = self._telemetry.metrics()
            publisher = self._publisher
            for name in (
                "published",
                "failed",
                "backpressure",
                "expired",
                "unconfirmed",
                "not_attempted",
                "connection_failed",
                "dropped",
                "client_submitted",
                "pubacked",
            ):
                observed = getattr(publisher, name, None)
                if isinstance(observed, int | float) and not isinstance(observed, bool):
                    values["collector.publisher." + name] = float(observed)
            workers = self._acquisition.workers
            values["collector.devices.total"] = float(len(workers))
            values["collector.devices.running"] = float(
                sum(worker.actual_state == "RUNNING" for worker in workers.values())
            )
            values["collector.devices.faulted"] = float(
                sum(worker.actual_state in {"FAULTED", "ISOLATED"} for worker in workers.values())
            )
        values.update(
            self._telemetry.percentiles(request.window_ms // 1000 if request.window_ms else 0)
        )
        if device_scope:
            prefix = f"collector.device.{device_scope}."
            values = {name: value for name, value in values.items() if name.startswith(prefix)}
            if not request.window_ms:
                worker = self._acquisition.workers[device_scope]
                values[prefix + "connected"] = float(worker.driver.is_open())
                values[prefix + "running"] = float(worker.actual_state == "RUNNING")
        now = datetime.now(UTC)
        response = pb.GetMetricsResponse()
        for name, value in sorted(values.items()):
            metric = response.metrics.add(
                name=name,
                value=value,
                unit=(
                    "milliseconds"
                    if "latency_ms" in name or name.endswith("_ms")
                    else "seconds"
                    if name.endswith("_seconds")
                    else "percent"
                    if name.endswith("_percent")
                    else "bytes"
                    if name.endswith("_bytes")
                    else "count"
                ),
                window_ms=request.window_ms,
            )
            metric.sampled_at.FromDatetime(now)
        return response

    async def GetEvents(  # noqa: N802
        self, request: pb.GetEventsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetEventsResponse:
        await self._authorize(context)
        since = request.from_time.ToDatetime(tzinfo=UTC) if request.HasField("from_time") else None
        until = request.to_time.ToDatetime(tzinfo=UTC) if request.HasField("to_time") else None
        if since is not None and until is not None and since > until:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid time interval")
        selection = (
            since.isoformat() if since else "",
            until.isoformat() if until else "",
            request.filter,
        )
        now = time.monotonic()
        for key, (expires, _, _) in tuple(self._event_pages.items()):
            if expires <= now:
                self._event_pages.pop(key, None)
        if request.page_token:
            try:
                identifier, offset_text = request.page_token.split(":", 1)
                offset = int(offset_text)
                if offset < 0:
                    raise ValueError("invalid offset")
            except ValueError:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            stored = self._event_pages.get(identifier)
            if stored is None:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "event page expired")
            assert stored is not None  # abort above is terminal
            _, events, original_selection = stored
            if original_selection != selection:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "page filter has changed")
        else:
            offset = 0
            events = self._telemetry.events(since=since, until=until, filter_text=request.filter)
            identifier = uuid.uuid4().hex
            if len(self._event_pages) >= 16:
                oldest = next(iter(self._event_pages))
                self._event_pages.pop(oldest, None)
            self._event_pages[identifier] = (now + 300.0, events, selection)
        if offset > len(events):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page offset")
        limit = min(request.page_size or 100, 500)
        response = pb.GetEventsResponse()
        for item in events[offset : offset + limit]:
            event = response.events.add(
                event_id=item.event_id,
                severity=item.severity,
                source=item.source,
                error_code=item.error_code,
                message=item.message,
            )
            event.timestamp.FromDatetime(item.timestamp)
        if offset + limit < len(events):
            response.next_page_token = f"{identifier}:{offset + limit}"
        else:
            self._event_pages.pop(identifier, None)
        return response

    async def Diagnose(  # noqa: N802
        self, request: pb.DiagnoseRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.DiagnoseResponse:
        await self._authorize(context)
        workers = self._acquisition.workers
        worker = workers.get(request.target_id) if request.target_id else None
        if request.target_id and worker is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown diagnostic target")
        stages = tuple(request.stages) or ("NETWORK", "PROTOCOL", "ACQUISITION", "NATS", "BOUNDARY")
        supported = {"NETWORK", "PROTOCOL", "ACQUISITION", "NATS", "BOUNDARY"}
        response = pb.DiagnoseResponse()
        results: list[str] = []
        deadline = time.monotonic() + min((request.timeout_ms or 10000) / 1000, 30.0)
        for name in stages:
            started = time.monotonic()
            status = "NOT_RUN"
            detail = "not evaluated"
            code: str | None = None
            evidence: list[tuple[str, str]] = []
            if name not in supported:
                detail = "unknown diagnostic stage"
            elif name == "BOUNDARY":
                status, detail = "PASS", "Collector evidence is not Sink persistence"
                response.limitations.append(
                    "NATS and protocol checks do not verify downstream Sink storage"
                )
            elif name == "NATS":
                connection = getattr(self._publisher, "_connection", None)
                if connection is not None and connection.is_connected:
                    status = "PASS"
                    detail = "NATS client transport connected"
                else:
                    status = "UNKNOWN"
                    code = "NATS_CONNECTION"
                    detail = "NATS transport is not currently connected"
                evidence.append(("delivery", str(getattr(self._publisher, "_delivery", ""))))
                # Connection checks never establish JetStream PubAck or Sink
                # persistence; say so explicitly (design §13.2 D4/D5).
                response.limitations.append(
                    "NATS stage verifies the client transport only; no isolated "
                    "publish, JetStream PubAck or Sink persistence is verified"
                )
            elif worker is None:
                detail = "no available device target"
            elif name == "NETWORK":
                endpoint = worker.plan.device.endpoint
                port = endpoint.port
                if port is None:
                    detail = "no effective TCP port"
                else:
                    remaining = max(0, deadline - time.monotonic())
                    if remaining == 0:
                        status, detail = "NOT_RUN", "diagnostic deadline expired"
                    else:
                        try:
                            _, writer = await asyncio.wait_for(
                                asyncio.open_connection(endpoint.host, port),
                                timeout=min(remaining, 3.0),
                            )
                            writer.close()
                            await writer.wait_closed()
                            status, detail = "PASS", "TCP port reachable"
                        except TimeoutError:
                            # A probe cut short by the diagnostic budget is not
                            # evidence that the endpoint is unreachable: report
                            # NOT_RUN honestly instead of fabricating a FAIL.
                            if time.monotonic() >= deadline:
                                status, detail = "NOT_RUN", "diagnostic deadline expired"
                            else:
                                status, detail = "FAIL", "TCP connect timed out"
                                code = "TIMEOUT"
                        except OSError as exc:
                            status, detail = "FAIL", str(exc)
                            # A refused port is reachable host but closed port;
                            # only routing/host failures are NETWORK.
                            code = "PORT" if exc.errno == errno.ECONNREFUSED else "NETWORK"
                    evidence.extend(
                        (
                            ("host", endpoint.host),
                            ("port", str(port)),
                        )
                    )
            elif name == "PROTOCOL":
                if not worker.driver.is_open():
                    detail = "protocol session is not running"
                else:
                    remaining = max(0, deadline - time.monotonic())
                    if remaining == 0:
                        detail = "diagnostic deadline expired"
                    else:
                        try:
                            healthy = await asyncio.wait_for(
                                asyncio.shield(worker.submit_health()),
                                timeout=min(remaining, 3.0),
                            )
                            status = "PASS" if healthy else "FAIL"
                            code = None if healthy else "PROTOCOL"
                            detail = (
                                "protocol health response"
                                if healthy
                                else (worker.last_error or "protocol health failed")
                            )
                        except TimeoutError:
                            # Budget expiry means the stage never actually ran
                            # to completion: NOT_RUN, not an UNKNOWN verdict.
                            if time.monotonic() >= deadline:
                                status, detail = "NOT_RUN", "diagnostic deadline expired"
                            else:
                                status, detail = "UNKNOWN", "protocol health not settled"
                                code = "TIMEOUT"
            elif name == "ACQUISITION":
                samples = worker.last_samples
                if not samples:
                    detail = "no captured sample in current process"
                else:
                    newest = max(sample.timestamp for sample in samples)
                    age_seconds = (datetime.now(UTC) - newest).total_seconds()
                    evidence.append(("newest_sample_age_ms", str(max(0, int(age_seconds * 1000)))))
                    # Cached observations cannot establish current device health.
                    # Design §13.5 applies the stricter (smaller) runtime bound.
                    if age_seconds > min(worker.timeout, worker.watchdog_interval):
                        status = "UNKNOWN"
                        detail = "cached samples are stale; acquisition freshness unverified"
                        results.append(status)
                        stage = response.stages.add(
                            stage=name,
                            status=status,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            message=detail,
                            source="collector",
                            error_code="STALE_EVIDENCE",
                        )
                        for key, value in evidence:
                            stage.evidence.add(name=key, value=value)
                        stage.observed_at.FromDatetime(datetime.now(UTC))
                        continue
                    good = sum(s.quality.value == "good" for s in samples)
                    encoded = 0
                    encoding_failed = False
                    encoder = getattr(self._publisher, "validate_encoding", None)
                    if encoder is not None:
                        for sample in samples:
                            try:
                                encoder(request.target_id, sample)
                            except (ValueError, TypeError) as exc:
                                encoding_failed = True
                                detail = f"sample encoding failed: {exc}"
                            else:
                                encoded += 1
                    if good == len(samples) and encoded == len(samples):
                        status = "PASS"
                    elif good != len(samples):
                        status, code = "FAIL", "POINT"
                    elif encoding_failed:
                        status, code = "FAIL", "NORMALIZATION"
                    else:
                        status = "UNKNOWN"
                    if not detail.startswith("sample encoding failed"):
                        detail = "recent sample quality and transport encoding"
                    evidence.extend(
                        (
                            ("total_points", str(len(samples))),
                            ("good_points", str(good)),
                            ("encoded_points", str(encoded)),
                        )
                    )
            results.append(status)
            stage = response.stages.add(
                stage=name,
                status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                message=detail,
                source="collector",
            )
            if status in {"FAIL", "UNKNOWN", "NOT_RUN"}:
                stage.error_code = code if status == "FAIL" and code else status
            for key, value in evidence:
                stage.evidence.add(name=key, value=value)
            stage.observed_at.FromDatetime(datetime.now(UTC))
        response.overall_status = (
            "UNKNOWN"
            if not results
            else "FAIL"
            if "FAIL" in results
            else "UNKNOWN"
            if "UNKNOWN" in results or "NOT_RUN" in results
            else "PASS"
        )
        if not request.target_id:
            response.limitations.append(
                "No target device specified; device chain stages were not run"
            )
        return response
