"""Implementation of the nine pre-existing CommonService RPCs."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from configuration_service.application.metrics import RpcMetrics
from configuration_service.application.service import ConfigurationApplication
from configuration_service.infrastructure.adapters.inbound.grpc.repository_server import (
    AuthenticationError,
    RepositoryServicer,
)
from configuration_service.settings import ServerSettings, load_settings
from contracts import common_pb2 as pb
from contracts import common_pb2_grpc as rpc

_LOG = logging.getLogger(__name__)


def _timestamp() -> Timestamp:
    value = Timestamp()
    value.FromDatetime(datetime.now(UTC))
    return value


class CommonServicer(rpc.CommonServiceServicer):
    """Common diagnostics and local bootstrap snapshot, never repository publication."""

    def __init__(
        self,
        app: ConfigurationApplication,
        repository_rpc: RepositoryServicer,
        settings: ServerSettings,
        bootstrap_path: Path,
        metrics: RpcMetrics,
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.repository_rpc = repository_rpc
        self.settings = settings
        self.bootstrap_path = bootstrap_path
        self.runtime_config = bootstrap_path.read_bytes()
        self.started_at = _timestamp()
        self._lock = threading.Lock()
        self._candidate: tuple[str, str, bytes] | None = None

    def _authorize(self, context: grpc.ServicerContext, role: str) -> str:
        try:
            return self.repository_rpc._actor(context, role)
        except (AuthenticationError, PermissionError) as exc:
            actor = "unauthenticated"
            try:
                self.app.repository.record_rejected_operation(
                    actor_id=actor,
                    operation="CommonService.Authorization",
                    error_code=type(exc).__name__,
                )
            except Exception:
                _LOG.exception("could not audit CommonService authorization failure")
            code = (
                grpc.StatusCode.UNAUTHENTICATED
                if isinstance(exc, AuthenticationError)
                else grpc.StatusCode.PERMISSION_DENIED
            )
            context.abort(code, str(exc))
            raise AssertionError("unreachable") from exc

    def GetServiceInfo(
        self, request: pb.Empty, context: grpc.ServicerContext
    ) -> pb.GetServiceInfoResponse:
        self._authorize(context, "query")
        return pb.GetServiceInfoResponse(
            service_id=self.settings.service_id,
            process_role="configuration-main",
            software_version="0.1.0",
            contract_version="windhub.configuration.v1",
            started_at=self.started_at,
        )

    def GetServiceStatus(
        self, request: pb.Empty, context: grpc.ServicerContext
    ) -> pb.GetServiceStatusResponse:
        self._authorize(context, "query")
        try:
            findings = self.app.repository.diagnose_storage()
        except Exception:
            findings = {"sqlite": (False, "repository access failed")}
        checks = [
            pb.DependencyHealth(
                name=name,
                health="HEALTHY" if healthy else "UNHEALTHY",
                detail=detail,
            )
            for name, (healthy, detail) in findings.items()
        ]
        return pb.GetServiceStatusResponse(
            health=(
                "HEALTHY" if all(healthy for healthy, _detail in findings.values()) else "UNHEALTHY"
            ),
            dependencies=checks,
            observed_at=_timestamp(),
        )

    def GetCapabilities(
        self, request: pb.Empty, context: grpc.ServicerContext
    ) -> pb.GetCapabilitiesResponse:
        self._authorize(context, "query")
        return pb.GetCapabilitiesResponse(
            rpc_names=[
                "CreateCandidate",
                "PublishCandidate",
                "ListConfigs",
                "ListConfigVersions",
                "GetConfig",
                "GetConfigBundle",
                "ListReferences",
                "SetRecommendedVersion",
                "DeprecateConfigVersion",
                "GetRepositoryStats",
                "CreateBackup",
                "GetBackupTask",
                "GetCurrentRevision",
                "GetPointTableBundle",
                "GetServiceInfo",
                "GetServiceStatus",
                "GetCapabilities",
                "GetConfigSnapshot",
                "PushConfigSnapshot",
                "ApplyConfigSnapshot",
                "GetMetrics",
                "GetEvents",
                "Diagnose",
            ],
            protocols=["grpc", "yaml", "sqlite"],
            features=[
                "immutable-config-revisions",
                "full-directory-manifests",
                "historical-point-table-bundles",
                "yaml-roundtrip",
                "sqlite-online-backup",
            ],
        )

    def GetConfigSnapshot(
        self, request: pb.GetConfigSnapshotRequest, context: grpc.ServicerContext
    ) -> pb.GetConfigSnapshotResponse:
        self._authorize(context, "admin")
        with self._lock:
            if request.scope == pb.SNAPSHOT_SCOPE_CANDIDATE:
                if self._candidate is None or self._candidate[0] != request.candidate_id:
                    context.abort(grpc.StatusCode.NOT_FOUND, "bootstrap candidate missing")
                data = self._candidate[2]
                revision = self._candidate[1]
            elif request.scope == pb.SNAPSHOT_SCOPE_DISK:
                data = self.bootstrap_path.read_bytes()
                revision = hashlib.sha256(data).hexdigest()
            elif request.scope == pb.SNAPSHOT_SCOPE_RUNTIME:
                data = self.runtime_config
                revision = hashlib.sha256(data).hexdigest()
            else:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid snapshot scope")
            return pb.GetConfigSnapshotResponse(
                snapshot_id=revision,
                revision=revision,
                files=[
                    pb.SnapshotFile(
                        relative_path=self.bootstrap_path.name,
                        content=data,
                        size_bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                ],
            )

    def PushConfigSnapshot(
        self, request: pb.PushConfigSnapshotRequest, context: grpc.ServicerContext
    ) -> pb.PushConfigSnapshotResponse:
        self._authorize(context, "admin")
        if len(request.files) != 1:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "one bootstrap YAML required")
        item = request.files[0]
        if item.relative_path != self.bootstrap_path.name:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unexpected bootstrap filename")
        if (
            len(item.content) != item.size_bytes
            or hashlib.sha256(item.content).hexdigest() != item.sha256
        ):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "snapshot hash mismatch")
        try:
            # Validation requires a real file path, but does not apply the new settings.
            candidate_path = self.bootstrap_path.with_name(
                f".{self.bootstrap_path.name}.{uuid.uuid4().hex}.candidate"
            )
            try:
                fd = os.open(candidate_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(item.content)
                load_settings(candidate_path)
            finally:
                candidate_path.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            return pb.PushConfigSnapshotResponse(
                valid=False,
                validation=[pb.ValidationIssue(path="bootstrap", code="INVALID", message=str(exc))],
            )
        with self._lock:
            current = hashlib.sha256(self.bootstrap_path.read_bytes()).hexdigest()
            if request.expected_revision != current:
                context.abort(grpc.StatusCode.ABORTED, "bootstrap revision changed")
            candidate_id = uuid.uuid4().hex
            self._candidate = (candidate_id, current, bytes(item.content))
            return pb.PushConfigSnapshotResponse(
                candidate_id=candidate_id, valid=True, base_revision=current
            )

    def ApplyConfigSnapshot(
        self, request: pb.ApplyConfigSnapshotRequest, context: grpc.ServicerContext
    ) -> pb.ApplyConfigSnapshotResponse:
        self._authorize(context, "admin")
        with self._lock:
            if self._candidate is None or self._candidate[0] != request.candidate_id:
                context.abort(grpc.StatusCode.NOT_FOUND, "bootstrap candidate missing")
            candidate_id, base, content = self._candidate
            current = hashlib.sha256(self.bootstrap_path.read_bytes()).hexdigest()
            if base != request.expected_revision or current != base:
                context.abort(grpc.StatusCode.ABORTED, "bootstrap revision changed")
            temporary = self.bootstrap_path.with_name(
                f".{self.bootstrap_path.name}.{candidate_id}.tmp"
            )
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.bootstrap_path)
            finally:
                temporary.unlink(missing_ok=True)
            self._candidate = None
            revision = hashlib.sha256(content).hexdigest()
            return pb.ApplyConfigSnapshotResponse(
                file_commit=revision,
                effective_revision=revision,
                restart_required=True,
                application_id=candidate_id,
                apply_results=[
                    pb.ApplicationResult(
                        object_id=self.settings.service_id,
                        status="RESTART_REQUIRED",
                        message="bootstrap YAML updated; process configuration unchanged",
                    )
                ],
            )

    def GetMetrics(
        self, request: pb.GetMetricsRequest, context: grpc.ServicerContext
    ) -> pb.GetMetricsResponse:
        self._authorize(context, "query")
        counts = self.app.repository.get_stats()
        counters = self.app.repository.operational_metrics()
        rpc = self.metrics.snapshot(window_ms=request.window_ms)
        recorded = {
            **{name: (float(value), "count") for name, value in counts.items()},
            **{name: (float(value), "count") for name, value in counters.items()},
            "rpc_request_count": (float(rpc.request_count), "count"),
            "rpc_failed_count": (float(rpc.failed_count), "count"),
            "rpc_requests_per_second": (rpc.rate_per_second, "requests/s"),
            "rpc_average_latency_ms": (rpc.average_latency_ms, "ms"),
            "rpc_p95_latency_ms": (rpc.p95_latency_ms, "ms"),
        }
        observed = _timestamp()
        return pb.GetMetricsResponse(
            metrics=[
                pb.Metric(
                    name=name,
                    value=value,
                    unit=unit,
                    window_ms=rpc.window_ms,
                    sampled_at=observed,
                )
                for name, (value, unit) in sorted(recorded.items())
            ]
        )

    def GetEvents(
        self, request: pb.GetEventsRequest, context: grpc.ServicerContext
    ) -> pb.GetEventsResponse:
        self._authorize(context, "query")
        after = None
        if request.page_token:
            token = self.repository_rpc._decode_cursor(request.page_token)
            if token.get("type") != "audit" or token.get("filter") != request.filter:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "event cursor changed")
            after = (str(token["at"]), str(token["event_id"]))
        beginning = (
            request.from_time.ToDatetime(tzinfo=UTC).isoformat()
            if request.HasField("from_time")
            else None
        )
        ending = (
            request.to_time.ToDatetime(tzinfo=UTC).isoformat()
            if request.HasField("to_time")
            else None
        )
        if beginning is not None and ending is not None and beginning > ending:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid event time range")
        if request.page_token and (token.get("from") != beginning or token.get("to") != ending):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "event range changed")
        rows = self.app.repository.list_audit_events(
            after=after,
            from_time=beginning,
            to_time=ending,
            operation_filter=request.filter,
            page_size=min(request.page_size or 100, self.app.limits.page_size),
        )
        output = [
            pb.Event(
                event_id=row["event_id"],
                timestamp=self._from_iso(str(row["created_at"])),
                severity="INFO" if row["outcome"] == "SUCCEEDED" else "ERROR",
                source="configuration",
                error_code="" if row["outcome"] == "SUCCEEDED" else row["outcome"],
                message=f"{row['operation']}: {row['target_id']}",
            )
            for row in rows
        ]
        cursor = ""
        if rows:
            cursor = self.repository_rpc._cursor(
                {
                    "type": "audit",
                    "filter": request.filter,
                    "from": beginning,
                    "to": ending,
                    "at": rows[-1]["created_at"],
                    "event_id": rows[-1]["event_id"],
                }
            )
        return pb.GetEventsResponse(events=output, next_page_token=cursor)

    @staticmethod
    def _from_iso(value: str) -> Timestamp:
        result = Timestamp()
        result.FromDatetime(datetime.fromisoformat(value))
        return result

    def Diagnose(
        self, request: pb.DiagnoseRequest, context: grpc.ServicerContext
    ) -> pb.DiagnoseResponse:
        self._authorize(context, "query")
        allowed = frozenset({"sqlite", "references", "wal", "storage"})
        selected = tuple(request.stages) if request.stages else tuple(sorted(allowed))
        unknown = set(selected) - allowed
        if unknown:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported diagnostic stage")
        try:
            findings = self.app.repository.diagnose_storage()
        except Exception:
            findings = {name: (False, "diagnostic access failed") for name in selected}
        stages = [
            pb.DiagnosticStage(
                stage=name,
                status="PASS" if findings[name][0] else "FAIL",
                error_code="" if findings[name][0] else "DIAGNOSTIC_FAILED",
                message=findings[name][1],
                source="configuration",
                observed_at=_timestamp(),
            )
            for name in selected
        ]
        return pb.DiagnoseResponse(
            overall_status="PASS" if all(stage.status == "PASS" for stage in stages) else "FAIL",
            stages=stages,
        )
