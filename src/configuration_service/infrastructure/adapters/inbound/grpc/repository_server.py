"""Authenticated gRPC adapters for the configuration repository."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from configuration_service.application.metrics import RpcMetrics
from configuration_service.application.service import (
    ConfigurationApplication,
    DocumentInput,
    DocumentOutput,
)
from configuration_service.domain.models import (
    ConfigIdentity,
    IdempotencyConflictError,
    RepositoryCapacityExceededError,
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryVersionExistsError,
    RevisionConflictError,
)
from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc

_LOG = logging.getLogger(__name__)
T = TypeVar("T")


class AuthenticationError(Exception):
    """Bearer token is missing or invalid."""


def _timestamp(value: str | None = None) -> Timestamp:
    result = Timestamp()
    moment = datetime.fromisoformat(value) if value else datetime.now(UTC)
    result.FromDatetime(moment)
    return result


def _identity(value: pb.ConfigId) -> ConfigIdentity:
    return ConfigIdentity(value.config_type, value.config_id)


def _config_id(value: ConfigIdentity) -> pb.ConfigId:
    return pb.ConfigId(config_type=value.config_type, config_id=value.config_id)


def _yaml_output(value: DocumentOutput) -> pb.ConfigYamlDocument:
    return pb.ConfigYamlDocument(
        config_id=_config_id(value.identity),
        version_id=value.version_id,
        schema_version=value.schema_version,
        file_version=value.file_version,
        revision_id=value.revision_id,
        file_name=value.file_name,
        yaml_bytes=value.yaml_bytes,
        content_sha256=value.content_sha256,
        yaml_sha256=value.yaml_sha256,
        schema_ref=value.schema_ref,
    )


def _error(exc: Exception, context: grpc.ServicerContext) -> None:
    if isinstance(exc, RevisionConflictError):
        context.abort(grpc.StatusCode.ABORTED, str(exc))
    if isinstance(exc, IdempotencyConflictError):
        context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
    if isinstance(exc, RepositoryVersionExistsError):
        context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
    if isinstance(exc, RepositoryCapacityExceededError):
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
    if isinstance(exc, RepositoryNotFoundError):
        context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
    if isinstance(exc, RepositoryConflictError):
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
    if isinstance(exc, ValueError):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
    if isinstance(exc, PermissionError):
        context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
    if isinstance(exc, AuthenticationError):
        context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
    if isinstance(exc, OSError | TimeoutError):
        context.abort(grpc.StatusCode.UNAVAILABLE, "repository temporarily unavailable")
    context.abort(grpc.StatusCode.INTERNAL, "unexpected repository error")


def guarded(
    operation: Callable[..., T],
) -> Callable[..., T]:
    """Map service errors to stable gRPC codes without leaking internal paths."""

    @wraps(operation)
    def call(self: Any, request: Any, context: grpc.ServicerContext) -> T:
        started = time.perf_counter()
        success = False
        try:
            response = operation(self, request, context)
            success = True
            return response
        except grpc.RpcError:
            raise
        except Exception as exc:
            try:
                actor = self.identify_actor(context)
            except AuthenticationError:
                actor = "unauthenticated"
            try:
                self.app.repository.record_rejected_operation(
                    actor_id=actor,
                    operation=operation.__name__,
                    error_code=type(exc).__name__,
                )
            except Exception:
                _LOG.exception("could not persist rejected RPC audit event")
            _error(exc, context)
            raise AssertionError("context.abort must terminate the RPC") from exc
        finally:
            self.metrics.observe(
                duration_ms=(time.perf_counter() - started) * 1000,
                success=success,
            )

    return call


class RepositoryServicer(rpc.ConfigurationRepositoryServiceServicer):
    """Serve repository RPCs with explicit bearer identity and permissions."""

    def __init__(
        self,
        application: ConfigurationApplication,
        *,
        permissions: dict[str, frozenset[str]],
        token_sources: dict[str, str],
        cursor_secret: bytes,
        submit_backup: Callable[[str], None],
        notify_revision: Callable[[int], None],
        metrics: RpcMetrics,
    ) -> None:
        self.app = application
        self.metrics = metrics
        self.submit_backup = submit_backup
        self.notify_revision = notify_revision
        self.permissions = permissions
        self.token_sources = token_sources
        live_tokens = [os.environ.get(name) for name in token_sources.values()]
        if any(not value or len(value) < 32 for value in live_tokens):
            raise ValueError("Configuration bearer credentials must be 32+ bytes")
        if len(set(live_tokens)) != len(live_tokens):
            raise ValueError("Configuration principals cannot share bearer credentials")
        if set(permissions) != set(token_sources):
            raise ValueError("token sources and permissions must match")
        if len(cursor_secret) < 32:
            raise ValueError("cursor secret must be at least 32 bytes")
        self.secret = cursor_secret

    def identify_actor(self, context: grpc.ServicerContext) -> str:
        metadata = dict(context.invocation_metadata() or ())
        value = metadata.get("authorization", "")
        if (
            not isinstance(value, str)
            or not value.startswith("Bearer ")
            or not 7 < len(value) <= 4103
        ):
            raise AuthenticationError("missing bearer authentication")
        candidate = value[7:]
        for actor, environment_name in self.token_sources.items():
            secret = os.environ.get(environment_name)
            if secret and hmac.compare_digest(candidate, secret):
                return actor
        raise AuthenticationError("bearer token identity not recognized")

    def _actor(self, context: grpc.ServicerContext, role: str) -> str:
        actor = self.identify_actor(context)
        permissions = self.permissions.get(actor, frozenset())
        if role not in permissions and "admin" not in permissions:
            raise PermissionError("missing repository permission")
        return actor

    def _cursor(self, value: dict[str, Any]) -> str:
        import base64

        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        digest = hmac.digest(self.secret, payload, "sha256")
        return base64.urlsafe_b64encode(digest + payload).decode("ascii")

    def _decode_cursor(self, token: str) -> dict[str, Any]:
        import base64

        try:
            raw = base64.b64decode(token.encode(), altchars=b"-_", validate=True)
            signature, data = raw[:32], raw[32:]
            if len(signature) != 32 or not hmac.compare_digest(
                signature, hmac.digest(self.secret, data, "sha256")
            ):
                raise ValueError("invalid cursor signature")
            document = json.loads(data)
            if not isinstance(document, dict):
                raise ValueError("invalid cursor payload")
            return document
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid pagination cursor") from exc

    @guarded
    def CreateCandidate(
        self, request: pb.CreateCandidateRequest, context: grpc.ServicerContext
    ) -> pb.CreateCandidateResponse:
        actor = self._actor(context, "publish")
        if not request.HasField("expected_revision_id"):
            raise ValueError("expected_revision_id must be provided")
        data = tuple(
            DocumentInput(_identity(item.config_id), item.file_name, item.yaml_bytes)
            for item in request.documents
        )
        if not request.request_id:
            raise ValueError("request_id is required")
        if len(request.documents) > self.app.limits.max_documents_per_publication:
            raise RepositoryCapacityExceededError("document count limit exceeded")
        if (
            sum(len(item.yaml_bytes) for item in request.documents)
            > self.app.limits.max_request_bytes
        ):
            raise RepositoryCapacityExceededError("candidate payload size limit exceeded")
        try:
            candidate_id, prepared = self.app.create_candidate(
                actor_id=actor,
                request_id=request.request_id,
                expected_revision=request.expected_revision_id,
                documents=data,
                validate_only=request.validate_only,
            )
        except ValueError as exc:
            return pb.CreateCandidateResponse(
                valid=False,
                base_revision_id=request.expected_revision_id,
                issues=[pb.ValidationIssue(error_code="SCHEMA_INVALID", message=str(exc))],
            )
        base_revision = request.expected_revision_id
        if candidate_id:
            staged = self.app.repository.get_candidate(candidate_id, actor)
            base_revision = int(staged["base_revision_id"] or 0)
        return pb.CreateCandidateResponse(
            candidate_id=candidate_id,
            valid=True,
            base_revision_id=base_revision,
            document_results=[
                pb.DocumentValidationResult(
                    config_id=_config_id(doc.identity),
                    schema_version=int(doc.canonical_payload["schema_version"]),
                    file_version=doc.file_version,
                    content_sha256=doc.content_sha256,
                )
                for doc in prepared
            ],
        )

    @guarded
    def PublishCandidate(
        self, request: pb.PublishCandidateRequest, context: grpc.ServicerContext
    ) -> pb.PublishCandidateResponse:
        actor = self._actor(context, "publish")
        if not request.HasField("expected_revision_id"):
            raise ValueError("expected revision is required")
        result = self.app.publish_candidate(
            actor_id=actor,
            request_id=request.request_id,
            expected_revision=request.expected_revision_id,
            candidate_id=request.candidate_id,
        )
        # Request ordering is the durable candidate document ordering.
        staged = self.app.repository.get_candidate(request.candidate_id, actor)
        docs = staged["documents"]
        items = [
            pb.ConfigVersionRef(
                config_id=pb.ConfigId(config_type=doc["config_type"], config_id=doc["config_id"]),
                version_id=version_id,
                file_version=doc["file_version"],
                content_sha256=doc["content_sha256"],
            )
            for doc, version_id in zip(docs, result.version_ids, strict=True)
        ]
        if not result.replayed:
            try:
                self.notify_revision(result.revision_id)
            except Exception:
                _LOG.warning("NATS hint enqueue failed; published revision remains durable")
        return pb.PublishCandidateResponse(
            revision_id=result.revision_id,
            published=items,
            committed_at=_timestamp(self.app.repository.get_revision_timestamp(result.revision_id)),
            already_applied=result.replayed,
        )

    @guarded
    def GetConfig(
        self, request: pb.GetConfigRequest, context: grpc.ServicerContext
    ) -> pb.GetConfigResponse:
        self._actor(context, "query")
        selector = request.WhichOneof("selector")
        if selector == "version_id":
            result = self.app.get_config(
                _identity(request.config_id), version_id=request.version_id
            )
        elif selector == "revision_id":
            result = self.app.get_config(
                _identity(request.config_id), revision_id=request.revision_id
            )
        else:
            raise ValueError("one configuration version selector is required")
        return pb.GetConfigResponse(document=_yaml_output(result))

    @guarded
    def GetConfigBundle(
        self, request: pb.GetConfigBundleRequest, context: grpc.ServicerContext
    ) -> pb.GetConfigBundleResponse:
        self._actor(context, "query")
        if not request.HasField("revision_id"):
            raise ValueError("revision_id required")
        result = self.app.get_bundle(
            tuple(_identity(root) for root in request.roots),
            request.revision_id,
            include_dependencies=(
                request.include_dependencies if request.HasField("include_dependencies") else True
            ),
        )
        digest = hashlib.sha256()
        for item in result:
            digest.update(item.version_id.encode())
            digest.update(item.yaml_sha256.encode())
        return pb.GetConfigBundleResponse(
            revision_id=request.revision_id,
            files=[_yaml_output(item) for item in result],
            manifest_sha256=digest.hexdigest(),
            total_bytes=sum(len(item.yaml_bytes) for item in result),
        )

    @guarded
    def ListConfigs(
        self, request: pb.ListConfigsRequest, context: grpc.ServicerContext
    ) -> pb.ListConfigsResponse:
        self._actor(context, "query")
        if request.page_token:
            token = self._decode_cursor(request.page_token)
            if token.get("type") != "configs":
                raise ValueError("invalid config cursor")
            revision = int(token["revision"])
            if request.HasField("revision_id") and request.revision_id != revision:
                raise ValueError("cursor belongs to another revision")
        else:
            revision = (
                request.revision_id
                if request.HasField("revision_id")
                else self.app.repository.current_revision()
            )
        if revision == 0 and not request.page_token:
            return pb.ListConfigsResponse(snapshot_revision_id=0)
        after = None
        if request.page_token:
            if token.get("revision") != revision:
                raise ValueError("cursor belongs to another revision")
            if token.get("filter") != [
                request.config_type,
                request.config_id_prefix,
                request.module_type,
            ]:
                raise ValueError("pagination filters changed")
            after = ConfigIdentity(token["after"][0], token["after"][1])
        rows = self.app.repository.list_configs(
            revision_id=revision,
            page_size=min(request.page_size or 100, self.app.limits.page_size) + 1,
            after=after,
            config_type=request.config_type,
            config_id_prefix=request.config_id_prefix,
            module_type=request.module_type,
        )
        page_limit = min(request.page_size or 100, self.app.limits.page_size)
        has_next = len(rows) > page_limit
        rows = rows[:page_limit]
        items = []
        for row in rows:
            schema = self.app.repository.get_document_schema(row["schema_ref"])
            items.append(
                pb.ConfigSummary(
                    config_id=pb.ConfigId(
                        config_type=row["config_type"], config_id=row["config_id"]
                    ),
                    recommended_version_id=row["version_id"],
                    recommended_file_version=row["file_version"],
                    schema_version=schema["document_schema_version"],
                    content_sha256=row["content_sha256"],
                    deprecated=bool(row["deprecated"]),
                )
            )
        cursor = ""
        if has_next:
            last = rows[-1]
            cursor = self._cursor(
                {
                    "type": "configs",
                    "revision": revision,
                    "filter": [request.config_type, request.config_id_prefix, request.module_type],
                    "after": [last["config_type"], last["config_id"]],
                }
            )
        return pb.ListConfigsResponse(
            items=items, snapshot_revision_id=revision, next_page_token=cursor
        )

    @guarded
    def ListConfigVersions(
        self, request: pb.ListConfigVersionsRequest, context: grpc.ServicerContext
    ) -> pb.ListConfigVersionsResponse:
        self._actor(context, "query")
        identity = _identity(request.config_id)
        watermark, revision = self.app.repository.version_snapshot(identity)
        after = 0
        if request.page_token:
            token = self._decode_cursor(request.page_token)
            if (
                token.get("type") != "versions"
                or token.get("config") != [identity.config_type, identity.config_id]
                or token.get("deprecated") != request.include_deprecated
            ):
                raise ValueError("version cursor does not match request")
            watermark, revision, after = (
                int(token["watermark"]),
                int(token["revision"]),
                int(token["after"]),
            )
        rows = self.app.repository.list_config_versions(
            identity,
            watermark=watermark,
            snapshot_revision=revision,
            page_size=min(request.page_size or 100, self.app.limits.page_size) + 1,
            after_rowid=after,
            include_deprecated=request.include_deprecated,
        )
        page_limit = min(request.page_size or 100, self.app.limits.page_size)
        has_next = len(rows) > page_limit
        rows = rows[:page_limit]
        items = []
        for row in rows:
            schema = self.app.repository.get_document_schema(row["schema_ref"])
            items.append(
                pb.ConfigVersionSummary(
                    version_id=row["version_id"],
                    file_version=row["file_version"],
                    schema_version=schema["document_schema_version"],
                    content_sha256=row["content_sha256"],
                    created_at=_timestamp(row["created_at"]),
                    deprecated=bool(row["deprecated"]),
                    referenced_by_count=row["referenced_by_count"],
                )
            )
        cursor = ""
        if has_next:
            cursor = self._cursor(
                {
                    "type": "versions",
                    "config": [identity.config_type, identity.config_id],
                    "deprecated": request.include_deprecated,
                    "watermark": watermark,
                    "revision": revision,
                    "after": rows[-1]["cursor"],
                }
            )
        return pb.ListConfigVersionsResponse(versions=items, next_page_token=cursor)

    @guarded
    def ListReferences(
        self, request: pb.ListReferencesRequest, context: grpc.ServicerContext
    ) -> pb.ListReferencesResponse:
        self._actor(context, "query")
        if request.direction not in (
            pb.REFERENCE_DIRECTION_OUTBOUND,
            pb.REFERENCE_DIRECTION_INBOUND,
        ):
            raise ValueError("reference direction required")
        inbound = request.direction == pb.REFERENCE_DIRECTION_INBOUND
        after = None
        if request.page_token:
            token = self._decode_cursor(request.page_token)
            if token.get("version") != request.version_id or token.get("inbound") != inbound:
                raise ValueError("reference cursor changed")
            after = tuple(token["after"])
        rows = self.app.repository.list_references(
            version_id=request.version_id,
            inbound=inbound,
            page_size=min(request.page_size or 100, self.app.limits.page_size) + 1,
            after=after,
        )
        page_limit = min(request.page_size or 100, self.app.limits.page_size)
        has_next = len(rows) > page_limit
        rows = rows[:page_limit]
        edges = []
        for row in rows:
            source = self.app.repository.get_version_identity(row["source_version_id"])
            target = self.app.repository.get_version_identity(row["target_version_id"])
            edges.append(
                pb.ReferenceEdge(
                    source_version_id=row["source_version_id"],
                    target_version_id=row["target_version_id"],
                    relation=row["relation"],
                    source_config_id=_config_id(source),
                    target_config_id=_config_id(target),
                )
            )
        cursor = ""
        if has_next:
            last = rows[-1]
            cursor = self._cursor(
                {
                    "version": request.version_id,
                    "inbound": inbound,
                    "after": [
                        last["source_version_id"],
                        last["target_version_id"],
                        last["relation"],
                    ],
                }
            )
        return pb.ListReferencesResponse(edges=edges, next_page_token=cursor)

    @guarded
    def SetRecommendedVersion(
        self, request: pb.SetRecommendedVersionRequest, context: grpc.ServicerContext
    ) -> pb.SetRecommendedVersionResponse:
        actor = self._actor(context, "publish")
        if not request.HasField("expected_revision_id"):
            raise ValueError("expected revision required")
        revision, previous = self.app.repository.set_recommended_version(
            actor_id=actor,
            request_id=request.request_id,
            identity=_identity(request.config_id),
            version_id=request.version_id,
            expected_revision=request.expected_revision_id,
        )
        result = pb.SetRecommendedVersionResponse(
            revision_id=revision,
            recommended_version_id=request.version_id,
            committed_at=_timestamp(self.app.repository.get_revision_timestamp(revision)),
        )
        if previous is not None:
            result.previous_version_id = previous
        try:
            self.notify_revision(revision)
        except Exception:
            _LOG.warning("recommended-version NATS hint unavailable")
        return result

    @guarded
    def DeprecateConfigVersion(
        self, request: pb.DeprecateConfigVersionRequest, context: grpc.ServicerContext
    ) -> pb.DeprecateConfigVersionResponse:
        actor = self._actor(context, "admin")
        if not request.HasField("expected_revision_id"):
            raise ValueError("expected revision required")
        revision, count = self.app.repository.deprecate_version(
            actor_id=actor,
            request_id=request.request_id,
            version_id=request.version_id,
            reason=request.reason,
            expected_revision=request.expected_revision_id,
        )
        try:
            self.notify_revision(revision)
        except Exception:
            _LOG.warning("deprecated-version NATS hint unavailable")
        return pb.DeprecateConfigVersionResponse(
            revision_id=revision,
            version_id=request.version_id,
            deprecated=True,
            referencing_versions_count=count,
            committed_at=_timestamp(self.app.repository.get_revision_timestamp(revision)),
        )

    @guarded
    def GetRepositoryStats(
        self, request: pb.GetRepositoryStatsRequest, context: grpc.ServicerContext
    ) -> pb.GetRepositoryStatsResponse:
        self._actor(context, "query")
        if request.scope not in (
            pb.REPOSITORY_STATS_SCOPE_ALL,
            pb.REPOSITORY_STATS_SCOPE_CONFIG_TYPE,
        ):
            raise ValueError("invalid stats scope")
        kind = request.config_type if request.scope == pb.REPOSITORY_STATS_SCOPE_CONFIG_TYPE else ""
        if request.scope == pb.REPOSITORY_STATS_SCOPE_CONFIG_TYPE and not kind:
            raise ValueError("config_type required for scoped stats")
        stats = self.app.repository.get_stats(config_type=kind)
        response = pb.GetRepositoryStatsResponse(
            document_count=stats["document_count"],
            version_count=stats["version_count"],
            revision_count=stats["revision_count"],
            reference_count=stats["reference_count"],
            db_schema_version=stats["db_schema_version"],
            observed_at=_timestamp(),
        )
        if request.include_storage:
            for name, value in self.app.repository.storage_stats().items():
                if value is None:
                    response.unavailable_metrics.append(name)
                else:
                    setattr(response, name, value)
        return response

    @guarded
    def CreateBackup(
        self, request: pb.CreateBackupRequest, context: grpc.ServicerContext
    ) -> pb.CreateBackupResponse:
        actor = self._actor(context, "admin")
        task = self.app.create_backup(actor, request.request_id, request.label)
        durable = self.app.repository.get_backup_task(task)
        if durable["state"] == "QUEUED":
            self.submit_backup(task)
        return pb.CreateBackupResponse(
            task_id=task,
            state=getattr(pb, f"BACKUP_STATE_{durable['state']}"),
            created_at=_timestamp(durable["created_at"]),
        )

    @guarded
    def GetBackupTask(
        self, request: pb.GetBackupTaskRequest, context: grpc.ServicerContext
    ) -> pb.GetBackupTaskResponse:
        self._actor(context, "admin")
        row = self.app.repository.get_backup_task(request.task_id)
        task = pb.BackupTask(
            task_id=row["task_id"],
            state=getattr(pb, f"BACKUP_STATE_{row['state']}"),
            created_at=_timestamp(row["created_at"]),
            path=row["path"] or "",
            size_bytes=row["size_bytes"] or 0,
            sha256=row["sha256"] or "",
            database_schema_version=row["database_schema_version"] or 0,
            integrity_verified=bool(row["integrity_verified"]),
            error_code="" if row["state"] == "SUCCEEDED" else row["state"],
            message=row["error"] or "",
        )
        if row["finished_at"]:
            task.finished_at.CopyFrom(_timestamp(row["finished_at"]))
        return pb.GetBackupTaskResponse(task=task)
