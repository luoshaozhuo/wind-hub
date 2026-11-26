"""Token-authorized gRPC adapters for the configuration repository."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from configuration_service.application.directory_manifest import (
    DirectoryFile,
    build_directory_manifest,
)
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
    """Required caller token is absent or invalid."""


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
        relative_path=value.relative_path,
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
    """Serve repository RPCs with mandatory per-caller token authorization."""

    def __init__(
        self,
        application: ConfigurationApplication,
        *,
        permissions: dict[str, frozenset[str]],
        tokens: dict[str, str],
        cursor_secret: bytes,
        submit_backup: Callable[[str], None],
        revision_notify: Callable[[int], None],
        metrics: RpcMetrics,
    ) -> None:
        self.app = application
        self.metrics = metrics
        self.submit_backup = submit_backup
        self.revision_notify = revision_notify
        self.permissions = permissions
        if not tokens or set(tokens) != set(permissions):
            raise ValueError("authorized callers and token identities must match")
        if len(set(tokens.values())) != len(tokens) or any(
            len(value) < 32 for value in tokens.values()
        ):
            raise ValueError("configuration access tokens must be unique and sufficiently long")
        self._tokens = dict(tokens)
        if len(cursor_secret) < 32:
            raise ValueError("cursor secret must be at least 32 bytes")
        self.secret = cursor_secret

    def _notify_revision(self, revision_id: int) -> None:
        """Broadcast an already-committed revision without weakening its durability."""
        try:
            self.revision_notify(revision_id)
        except Exception:
            _LOG.exception(
                "could not schedule NATS revision notice: revision=%s",
                revision_id,
            )

    def _actor(self, context: grpc.ServicerContext, role: str) -> str:
        metadata = context.invocation_metadata()
        authorization = [item[1] for item in metadata if item[0].lower() == "authorization"]
        if (
            len(authorization) != 1
            or not isinstance(authorization[0], str)
            or not authorization[0].startswith("Bearer ")
        ):
            raise AuthenticationError("valid authorization token required")
        supplied = authorization[0][7:]
        if len(supplied) < 32:
            raise AuthenticationError("valid authorization token required")
        matched = [
            actor
            for actor, expected in self._tokens.items()
            if hmac.compare_digest(supplied, expected)
        ]
        if len(matched) != 1:
            raise AuthenticationError("valid authorization token required")
        actor = matched[0]
        roles = self.permissions.get(actor, frozenset())
        if role not in roles and "admin" not in roles:
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
            DocumentInput(
                _identity(item.config_id),
                item.file_name,
                item.yaml_bytes,
                relative_path=item.relative_path,
            )
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
        manifest = build_directory_manifest(
            tuple(DirectoryFile(item.relative_path, item.yaml_bytes) for item in request.documents),
            max_documents=self.app.limits.max_documents_per_publication,
            max_bytes=self.app.limits.max_request_bytes,
        )
        return pb.CreateCandidateResponse(
            candidate_id=candidate_id,
            valid=True,
            base_revision_id=request.expected_revision_id,
            manifest_sha256=manifest.sha256,
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
        # A committed revision survives notification failure. Offline subscribers
        # reconcile with the repository on start, reconnect and periodic refresh.
        if not result.replayed:
            self._notify_revision(result.revision_id)
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
        return pb.PublishCandidateResponse(
            revision_id=result.revision_id,
            published=items,
            committed_at=_timestamp(self.app.repository.get_revision_timestamp(result.revision_id)),
            already_applied=result.replayed,
        )

    @guarded
    def GetCurrentRevision(
        self, request: pb.GetCurrentRevisionRequest, context: grpc.ServicerContext
    ) -> pb.GetCurrentRevisionResponse:
        del request
        self._actor(context, "query")
        revision, manifest, committed = self.app.repository.get_current_manifest()
        response = pb.GetCurrentRevisionResponse(revision_id=revision, manifest_sha256=manifest)
        if committed is not None:
            response.committed_at.CopyFrom(_timestamp(committed))
        return response

    @guarded
    def GetServiceConfigBundle(
        self, request: pb.GetServiceConfigBundleRequest, context: grpc.ServicerContext
    ) -> pb.GetServiceConfigBundleResponse:
        self._actor(context, "query")
        if not request.deployment_id or not request.service_id:
            raise ValueError("deployment_id and service_id are required")
        revision, target, documents = self.app.get_service_config_bundle(
            request.deployment_id,
            request.service_id,
            request.revision_id or None,
        )
        manifest = hashlib.sha256()
        for item in documents:
            for value in (item.relative_path, item.version_id, item.yaml_sha256):
                encoded = value.encode("utf-8")
                manifest.update(len(encoded).to_bytes(4, "big"))
                manifest.update(encoded)
        return pb.GetServiceConfigBundleResponse(
            repository_revision_id=revision,
            target_revision=target,
            files=[_yaml_output(item) for item in documents],
            manifest_sha256=manifest.hexdigest(),
            total_bytes=sum(len(item.yaml_bytes) for item in documents),
        )

    @guarded
    def GetPointTableBundle(
        self, request: pb.GetPointTableBundleRequest, context: grpc.ServicerContext
    ) -> pb.GetPointTableBundleResponse:
        self._actor(context, "query")
        documents, manifest = self.app.get_point_table_bundle(
            request.point_table_id,
            expected_manifest_sha256=request.expected_manifest_sha256 or None,
        )
        root = [
            item
            for item in documents
            if item.identity.config_type == "point_table"
            and request.point_table_id
            == f"point_table_{item.identity.config_id}_v{item.file_version}"
        ]
        if len(root) != 1:
            raise RepositoryConflictError("immutable PointTable root is ambiguous")
        typed_rows = self.app.repository.get_projection_rows(root[0].version_id, "point_tables")
        if len(typed_rows) != 1:
            raise RepositoryConflictError("PointTable typed identity is incomplete")
        return pb.GetPointTableBundleResponse(
            point_table_id=request.point_table_id,
            device_type=str(typed_rows[0]["device_type"] or ""),
            documents=[_yaml_output(item) for item in documents],
            manifest_sha256=manifest,
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
        exact = tuple(request.exact_roots)
        dependencies = (
            request.include_dependencies if request.HasField("include_dependencies") else True
        )
        if exact:
            if request.roots or request.HasField("revision_id"):
                raise ValueError("exact_roots cannot be combined with roots/revision_id")
            if not request.HasField("include_dependencies") or not dependencies:
                raise ValueError("exact_roots require complete dependencies")
            resolved_roots: list[tuple[ConfigIdentity, str]] = []
            for root in exact:
                identity = _identity(root.config_id)
                if not root.version_id and not root.file_version:
                    raise ValueError("exact_roots require version_id or file_version")
                version_id = root.version_id
                if root.file_version:
                    pinned = self.app.repository.resolve_version(identity, root.file_version)
                    if version_id and version_id != pinned:
                        raise RepositoryConflictError("exact root selectors disagree")
                    version_id = pinned
                resolved_roots.append((identity, version_id))
            revision_id, result = self.app.get_exact_bundle_snapshot(
                tuple(resolved_roots), include_dependencies=True
            )
            for root, (identity, version_id) in zip(exact, resolved_roots, strict=True):
                if root.content_sha256 and not any(
                    item.identity == identity
                    and item.version_id == version_id
                    and item.content_sha256 == root.content_sha256
                    for item in result
                ):
                    raise RepositoryConflictError("exact root content digest mismatch")
        else:
            if not request.roots or not request.HasField("revision_id"):
                raise ValueError("roots and revision_id are required together")
            result = self.app.get_bundle(
                tuple(_identity(root) for root in request.roots),
                request.revision_id,
                include_dependencies=dependencies,
            )
            revision_id = request.revision_id
        digest = hashlib.sha256()
        for item in result:
            # The manifest authenticates the exact path, immutable version
            # and bytes, not merely the document's logical identity.
            for value in (item.relative_path, item.version_id, item.yaml_sha256):
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(4, "big"))
                digest.update(encoded)
        return pb.GetConfigBundleResponse(
            revision_id=revision_id,
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
        prior_revision = self.app.repository.current_revision()
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
        if revision > prior_revision:
            self._notify_revision(revision)
        return result

    @guarded
    def DeprecateConfigVersion(
        self, request: pb.DeprecateConfigVersionRequest, context: grpc.ServicerContext
    ) -> pb.DeprecateConfigVersionResponse:
        actor = self._actor(context, "admin")
        if not request.HasField("expected_revision_id"):
            raise ValueError("expected revision required")
        prior_revision = self.app.repository.current_revision()
        revision, count = self.app.repository.deprecate_version(
            actor_id=actor,
            request_id=request.request_id,
            version_id=request.version_id,
            reason=request.reason,
            expected_revision=request.expected_revision_id,
        )
        if revision > prior_revision:
            self._notify_revision(revision)
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
