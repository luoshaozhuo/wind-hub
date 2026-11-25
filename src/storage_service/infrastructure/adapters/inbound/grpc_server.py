"""StorageService gRPC handlers: typed Contracts RPC and scoped token authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, NoReturn, TypeVar, cast

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import storage_service_pb2 as pb
from contracts import storage_service_pb2_grpc as rpc
from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.query import (
    HistoryFilter,
    HistoryQueryService,
    HistoryReplayUnavailableError,
    QueryRejectedError,
)
from storage_service.application.tasks import AsyncStorageTasks, ExportQuotaExceededError
from storage_service.application.wide_query import WideHistoryQueryService, WideHistoryFilter
from storage_service.application.wide_aggregate import WideAggregationService
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
from storage_service.domain.sample import DataLevel
from storage_service.domain.tasks import TaskRecord
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
)
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import (
    MetadataConflictError,
    MetadataStore,
)
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)

T = TypeVar("T")
_LOG = logging.getLogger(__name__)

_AGGREGATE_SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
_AGGREGATE_SNAPSHOT_MAX_ENTRIES = 16


def _date(us: int) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us))
    return result


def _optional_date(value: str | None) -> Timestamp | None:
    if value is None:
        return None
    result = Timestamp()
    result.FromDatetime(datetime.fromisoformat(value))
    return result


def _us(timestamp: Timestamp) -> int:
    if timestamp.nanos % 1000 != 0:
        raise QueryRejectedError("timestamp exceeds TDengine microsecond precision")
    return timestamp.seconds * 1_000_000 + timestamp.nanos // 1000


def _filter(source: pb.StorageFilter) -> HistoryFilter:
    levels = {
        pb.STORAGE_DATA_LEVEL_RAW: DataLevel.RAW,
        pb.STORAGE_DATA_LEVEL_PROTOCOL: DataLevel.PROTOCOL,
        pb.STORAGE_DATA_LEVEL_BUSINESS: DataLevel.BUSINESS,
    }
    if source.data_level not in levels:
        raise QueryRejectedError("query must select exactly one explicit data_level")
    if not source.HasField("start_time") or not source.HasField("end_time"):
        raise QueryRejectedError("both query timestamps are mandatory")
    return HistoryFilter(
        wind_farm_id=source.wind_farm_id,
        data_level=levels[source.data_level],
        start_us=_us(source.start_time),
        end_us=_us(source.end_time),
        device_type=source.device_type,
        device_id=source.device_id,
        point_table_id=source.point_table_id,
        point_id=source.point_id,
        business_point_id=source.business_point_id,
        qualities=tuple(source.qualities),
    )


class AuthenticationError(PermissionError):
    """Bearer credentials are absent or invalid."""


class AuthorizationError(PermissionError):
    """An authenticated caller lacks the required farm or operation scope."""


class AuthorizationPolicy:
    """Explicitly scoped tokens, with opt-in sanitized public health endpoints."""

    def __init__(self, config: StorageConfig) -> None:
        self._callers = config.auth.callers
        self._farms = frozenset(config.allowed_wind_farms)
        self._allow_unauthenticated_status = config.grpc.allow_unauthenticated_status
        supplied = [os.environ.get(caller.token_env) for caller in self._callers]
        if any(not token or len(token) < 32 for token in supplied):
            raise ValueError("all Storage caller tokens require 32+ bytes")
        if len(set(supplied)) != len(supplied):
            raise ValueError("Storage caller tokens must be distinct")

    def allow_public_status(self, context: grpc.aio.ServicerContext[Any, Any]) -> bool:
        """Only an explicitly enabled, credential-free status request is public."""
        if not self._allow_unauthenticated_status:
            return False
        headers = dict(context.invocation_metadata() or ())
        # A supplied but invalid credential is never treated as anonymous.
        return "authorization" not in headers

    def require(
        self,
        context: grpc.aio.ServicerContext[Any, Any],
        *,
        permission: str,
        farm: str | None,
        global_scope: bool = False,
    ) -> str:
        headers = dict(context.invocation_metadata() or ())
        header = headers.get("authorization", "")
        if not isinstance(header, str) or not header.startswith("Bearer "):
            raise AuthenticationError("missing bearer authentication")
        candidate = header[7:]
        if not candidate or len(candidate) > 4096:
            raise AuthenticationError("invalid bearer token")
        for caller in self._callers:
            token = os.environ.get(caller.token_env)
            if not token or not hmac.compare_digest(candidate, token):
                continue
            authorized = permission in caller.permissions or "storage.admin" in caller.permissions
            if not authorized:
                raise AuthorizationError("missing Storage permission")
            if farm is None:
                if global_scope and "storage.admin" not in caller.permissions:
                    raise AuthorizationError("global operation requires Storage admin")
            elif farm not in self._farms:
                raise AuthorizationError("Storage is not provisioned for this wind farm")
            elif farm not in caller.wind_farms and not (
                "*" in caller.wind_farms and "storage.admin" in caller.permissions
            ):
                raise AuthorizationError("Storage farm scope does not match")
            return caller.token_env
        raise AuthenticationError("invalid bearer token")


def guarded(
    operation: Callable[..., Coroutine[Any, Any, T]],
) -> Callable[..., Coroutine[Any, Any, T]]:
    """Fail closed with stable gRPC statuses, hiding internal SQL and file paths."""

    @wraps(operation)
    async def call(
        self: StorageServicer,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> T:
        try:
            return await operation(self, request, context)
        except grpc.RpcError:
            raise
        except AuthenticationError:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Storage authentication failed")
            raise AssertionError("abort must terminate RPC") from None
        except AuthorizationError:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "Storage authorization denied")
            raise AssertionError("abort must terminate RPC") from None
        except (QueryRejectedError, ValueError):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid Storage request")
            raise AssertionError("abort must terminate RPC") from None
        except MetadataConflictError:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "Storage idempotency conflict")
            raise AssertionError("abort must terminate RPC") from None
        except HistoryReplayUnavailableError:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE, "Storage history is replaying Kafka offsets"
            )
            raise AssertionError("abort must terminate RPC") from None
        except TDengineSchemaConflictError as exc:
            self._metrics.record_schema_conflict()
            _LOG.warning("Storage physical/history schema rejected: %s", exc)
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Storage physical schema or historical conflict is invalid",
            )
            raise AssertionError("abort must terminate RPC") from None
        except ConfigHistoryUnavailableError:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "historical Configuration closure is unavailable",
            )
            raise AssertionError("abort must terminate RPC") from None
        except ExportQuotaExceededError:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Storage quota exceeded")
            raise AssertionError("abort must terminate RPC") from None
        except (OSError, TimeoutError, ConnectionError):
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Storage dependency unavailable")
            raise AssertionError("abort must terminate RPC") from None
        except RuntimeError:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Storage operation not ready")
            raise AssertionError("abort must terminate RPC") from None

    return call


def _task(record: TaskRecord) -> pb.StorageTask:
    code = pb.StorageTaskState.Value(f"STORAGE_TASK_STATE_{record.state}")
    result = pb.StorageTask(
        task_id=record.task_id,
        state=code,
        artifact_id=record.artifact_id,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        error_code=record.error_code,
        message=record.message,
    )
    created = _optional_date(record.created_at)
    finished = _optional_date(record.finished_at)
    if created is not None:
        result.created_at.CopyFrom(created)
    if finished is not None:
        result.finished_at.CopyFrom(finished)
    return result


class StorageServicer(rpc.StorageServiceServicer, common_rpc.CommonServiceServicer):
    """Storage transport delegates all persistence and semantic policy to services."""

    def __init__(
        self,
        *,
        config: StorageConfig,
        metadata: MetadataStore,
        query: HistoryQueryService,
        wide_query: WideHistoryQueryService,
        wide_aggregation: WideAggregationService,
        jobs: AsyncStorageTasks,
        point_tables: ConfigurationPointTableResolver,
        consumer: KafkaStorageConsumer,
        cursor_secret: bytes,
        config_status: Callable[[], str],
        metrics: StorageRuntimeMetrics,
        config_revisions: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self._auth = AuthorizationPolicy(config)
        self._config = config
        self._metadata = metadata
        self._query = query
        self._wide_query = wide_query
        self._wide_aggregation = wide_aggregation
        self._jobs = jobs
        self._point_tables = point_tables
        self._consumer = consumer
        if len(cursor_secret) < 32:
            raise ValueError("Storage cursor secret needs 256 bits")
        self._secret = cursor_secret
        self._config_status = config_status
        self._config_revisions = config_revisions
        self._metrics = metrics
        self._started_at = datetime.now(UTC)
        self._aggregate_snapshots: dict[str, tuple[float, str, tuple[bytes, ...]]] = {}

    @guarded
    async def GetServiceInfo(
        self,
        request: common.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetServiceInfoResponse:
        is_public = self._auth.allow_public_status(context)
        if not is_public:
            self._auth.require(context, permission="storage.inspect", farm=None)
        response = common.GetServiceInfoResponse(
            service_id=self._config.service_id,
            process_role="storage",
            software_version="0.1.0",
            contract_version="windhub.storage.v1",
        )
        response.started_at.FromDatetime(self._started_at)
        return response

    @guarded
    async def GetServiceStatus(
        self,
        request: common.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetServiceStatusResponse:
        is_public = self._auth.allow_public_status(context)
        if not is_public:
            self._auth.require(context, permission="storage.inspect", farm=None)
        status = self._config_status()
        response = common.GetServiceStatusResponse(
            health="HEALTHY" if status == "READY" else "DEGRADED",
            dependencies=[
                common.DependencyHealth(
                    name="configuration",
                    health="HEALTHY" if status == "READY" else "UNHEALTHY",
                    detail="" if is_public else status,
                )
            ],
        )
        if not is_public and self._config_revisions is not None:
            target_revision, active_revision = self._config_revisions()
            response.target_revision = target_revision
            response.active_revision = active_revision
        response.observed_at.FromDatetime(datetime.now(UTC))
        return response

    @guarded
    async def GetCapabilities(
        self,
        request: common.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetCapabilitiesResponse:
        is_public = self._auth.allow_public_status(context)
        if not is_public:
            self._auth.require(context, permission="storage.inspect", farm=None)
        return common.GetCapabilitiesResponse(
            rpc_names=[
                "GetServiceInfo",
                "GetServiceStatus",
                "GetCapabilities",
                "QuerySamples",
                "QueryAggregates",
                "CreateExport",
                "GetExportTask",
                "CreateBackup",
                "GetBackupTask",
                "GetStorageStats",
                "ListBlockedPartitions",
                "RetryBlockedPartition",
            ],
            protocols=[],
            features=["history-query", "csv-export", "native-backup", "kafka-ingestion"],
        )

    async def _unsupported_common(
        self, context: grpc.aio.ServicerContext[Any, Any], permission: str
    ) -> NoReturn:
        self._auth.require(context, permission=permission, farm=None)
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "Storage does not implement this CommonService operation",
        )
        raise AssertionError("abort must terminate RPC")

    @guarded
    async def GetConfigSnapshot(
        self,
        request: common.GetConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetConfigSnapshotResponse:
        return await self._unsupported_common(context, "storage.inspect")

    @guarded
    async def PushConfigSnapshot(
        self,
        request: common.PushConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.PushConfigSnapshotResponse:
        return await self._unsupported_common(context, "storage.operate")

    @guarded
    async def ApplyConfigSnapshot(
        self,
        request: common.ApplyConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.ApplyConfigSnapshotResponse:
        return await self._unsupported_common(context, "storage.operate")

    @guarded
    async def GetMetrics(
        self,
        request: common.GetMetricsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetMetricsResponse:
        return await self._unsupported_common(context, "storage.inspect")

    @guarded
    async def GetEvents(
        self,
        request: common.GetEventsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetEventsResponse:
        return await self._unsupported_common(context, "storage.inspect")

    @guarded
    async def Diagnose(
        self,
        request: common.DiagnoseRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.DiagnoseResponse:
        return await self._unsupported_common(context, "storage.inspect")

    @guarded
    async def QueryWideRows(
        self,
        request: pb.QueryWideRowsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QueryWideRowsResponse:
        """Return frozen device-wide columns and quality without cross-version joins."""
        self._auth.require(context, permission="storage.read", farm=request.wind_farm_id)
        if self._consumer.recovering:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE, "Storage Kafka replay is not settled"
            )
        levels = {
            pb.STORAGE_DATA_LEVEL_RAW: WideDataLevel.RAW,
            pb.STORAGE_DATA_LEVEL_PROTOCOL: WideDataLevel.PROTOCOL,
            pb.STORAGE_DATA_LEVEL_BUSINESS: WideDataLevel.BUSINESS,
        }
        level = levels.get(request.data_level)
        if level is None or not request.HasField("start_time") or not request.HasField("end_time"):
            raise QueryRejectedError("wide queries require explicit level and UTC time boundaries")
        identity = WideIdentity(
            wind_farm_id=request.wind_farm_id,
            device_config_id=request.device_config_id,
            device_id=request.device_id,
            data_level=level,
        )
        schema = await self._point_tables.resolve_wide_schema(
            identity,
            type_mapping=self._config.tdengine.type_mapping,
            max_row_bytes=65536,
        )
        point_ids = tuple(request.point_ids) or tuple(
            column.point_id for column in schema.columns
        )
        filter_ = WideHistoryFilter(
            wind_farm_id=identity.wind_farm_id,
            device_id=identity.device_id,
            device_config_id=identity.device_config_id,
            data_level=level,
            start_time_us=_us(request.start_time),
            end_time_us=_us(request.end_time),
            point_ids=point_ids,
            limit=request.page_size or 200,
            qualities=tuple(request.qualities),
        )
        page = await self._wide_query.query(
            schema=schema,
            filter_=filter_,
            device_type=self._point_tables.wide_device_type(identity),
            page_token=request.page_token,
        )
        if self._consumer.recovering:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE, "Storage replay began during query"
            )
        response = pb.QueryWideRowsResponse(
            wind_farm_id=identity.wind_farm_id,
            device_id=identity.device_id,
            device_config_id=identity.device_config_id,
            point_table_id=schema.point_table_id,
            point_table_version=schema.point_table_version,
            data_level=request.data_level,
            schema_fingerprint=schema.fingerprint,
            next_page_token=page.next_page_token,
            complete=page.complete,
        )
        columns = {
            column.point_id: (index, column)
            for index, column in enumerate(schema.columns)
        }
        for point_id in point_ids:
            index, column = columns[point_id]
            unit = await self._point_tables.get_wide_unit(
                point_table_id=schema.point_table_id,
                point_id=point_id,
                data_level=level,
            )
            response.columns.add(
                point_id=point_id,
                physical_column_name=column.column_name,
                sql_type=column.sql_type,
                quality_ordinal=index,
                unit=unit,
            )
        for row in page.rows:
            result = response.rows.add()
            result.sample_time.CopyFrom(_date(row.sample_time_us))
            if len(row.points) != len(point_ids):
                raise TDengineSchemaConflictError("wide query returned a partial row")
            for expected, (point_id, value, quality) in zip(
                point_ids, row.points, strict=True
            ):
                if point_id != expected:
                    raise TDengineSchemaConflictError("wide row column order mismatch")
                physical = columns[point_id][1]
                point = result.points.add(point_id=point_id, quality=int(quality))
                if value is None:
                    continue
                kind = physical.sql_type
                if kind == "BOOL":
                    if type(value) is not bool:
                        raise TDengineSchemaConflictError("wide BOOL invalid")
                    point.bool_value = value
                elif kind in {"TINYINT", "SMALLINT", "INT", "BIGINT"}:
                    if type(value) is not int:
                        raise TDengineSchemaConflictError("wide signed integer invalid")
                    point.signed_value = value
                elif kind.endswith(" UNSIGNED"):
                    if type(value) is not int or value < 0:
                        raise TDengineSchemaConflictError("wide unsigned integer invalid")
                    point.unsigned_value = value
                elif kind in {"FLOAT", "DOUBLE"}:
                    if type(value) not in {int, float}:
                        raise TDengineSchemaConflictError("wide float invalid")
                    point.float_value = float(value)
                elif kind.startswith("VARCHAR("):
                    if not isinstance(value, str):
                        raise TDengineSchemaConflictError("wide text invalid")
                    point.string_value = value
                elif kind.startswith("VARBINARY("):
                    if not isinstance(value, bytes | bytearray | memoryview):
                        raise TDengineSchemaConflictError("wide binary invalid")
                    point.bytes_value = bytes(value)
                else:
                    raise TDengineSchemaConflictError("unsupported wide physical field type")
        return response

    @guarded
    async def QuerySamples(
        self,
        request: pb.QuerySamplesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QuerySamplesResponse:
        self._auth.require(context, permission="storage.read", farm=request.filter.wind_farm_id)
        if self._consumer.recovering:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Storage history is unavailable during Kafka replay reconciliation",
            )
        filter_ = _filter(request.filter)
        page = await self._query.query(
            filter_,
            page_size=request.page_size or 200,
            page_token=request.page_token,
        )
        reply = pb.QuerySamplesResponse(
            next_page_token=page.next_page_token, complete=page.complete
        )
        for row in page.rows:
            farm, device_type, device_id, table_id, level, _ = row.identity
            if farm != filter_.wind_farm_id or level != filter_.data_level.value:
                raise TDengineSchemaConflictError("history identity disagrees with query")
            unit = await self._point_tables.get_unit(
                point_table_id=table_id,
                point_id=row.point_id,
                business_point_id=row.business_point_id,
                data_level=filter_.data_level,
            )
            sample = reply.items.add(
                wind_farm_id=farm,
                device_type=device_type,
                device_id=device_id,
                point_table_id=table_id,
                point_id=row.point_id,
                business_point_id=row.business_point_id,
                sample_id=row.sample_id,
                data_level=request.filter.data_level,
                value_kind=row.value_kind,
                quality=row.quality,
                unit=unit,
                logical_key=row.logical_key,
                content_sha256=row.content_sha256,
            )
            sample.sample_time.CopyFrom(_date(row.ts_us))
            sample.publish_time.CopyFrom(_date(row.publish_time_us))
            if row.receive_time_us is not None:
                sample.receive_time.CopyFrom(_date(row.receive_time_us))
            if row.value_kind == pb.STORAGE_VALUE_KIND_INT64:
                sample.value_i64 = int(cast(int, row.value))
            elif row.value_kind == pb.STORAGE_VALUE_KIND_UINT64:
                sample.value_u64 = int(cast(int, row.value))
            elif row.value_kind == pb.STORAGE_VALUE_KIND_DOUBLE:
                sample.value_f64 = float(cast(int | float, row.value))
            elif row.value_kind == pb.STORAGE_VALUE_KIND_BOOL:
                sample.value_bool = bool(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_TEXT:
                sample.value_text = str(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_BYTES:
                sample.payload_bytes = bytes(cast(bytes, row.value))
            elif row.value_kind != pb.STORAGE_VALUE_KIND_MISSING:
                raise TDengineSchemaConflictError("unknown stored value kind")
        return reply

    async def _query_wide_aggregates(
        self, request: pb.QueryAggregatesRequest,
    ) -> pb.QueryAggregatesResponse:
        """Aggregate one exact device/schema with integer-preserving output."""
        source = request.wide_filter
        levels = {
            pb.STORAGE_DATA_LEVEL_RAW: WideDataLevel.RAW,
            pb.STORAGE_DATA_LEVEL_PROTOCOL: WideDataLevel.PROTOCOL,
            pb.STORAGE_DATA_LEVEL_BUSINESS: WideDataLevel.BUSINESS,
        }
        level = levels.get(source.data_level)
        if (
            level is None or not source.HasField("start_time")
            or not source.HasField("end_time")
            or source.page_token
        ):
            raise QueryRejectedError("invalid wide aggregation source")
        operations = {
            pb.STORAGE_AGGREGATION_COUNT: "count",
            pb.STORAGE_AGGREGATION_MIN: "min",
            pb.STORAGE_AGGREGATION_MAX: "max",
            pb.STORAGE_AGGREGATION_AVG: "avg",
        }
        operation = operations.get(request.aggregation)
        if operation is None:
            raise QueryRejectedError("wide aggregation requires count/min/max/avg")
        identity = WideIdentity(
            wind_farm_id=source.wind_farm_id,
            device_config_id=source.device_config_id,
            device_id=source.device_id,
            data_level=level,
        )
        schema = await self._point_tables.resolve_wide_schema(
            identity, type_mapping=self._config.tdengine.type_mapping,
            max_row_bytes=65536,
        )
        points = tuple(source.point_ids) or tuple(
            column.point_id for column in schema.columns
        )
        filter_ = WideHistoryFilter(
            wind_farm_id=identity.wind_farm_id,
            device_id=identity.device_id,
            device_config_id=identity.device_config_id,
            data_level=level,
            start_time_us=_us(source.start_time),
            end_time_us=_us(source.end_time),
            point_ids=points,
            limit=source.page_size or 200,
            qualities=tuple(source.qualities),
        )
        filter_.validate(schema, maximum=self._config.query.max_page_size)
        page = await self._wide_aggregation.page(
            schema=schema, filter_=filter_,
            device_type=self._point_tables.wide_device_type(identity),
            operation=operation,
            window_ms=request.window_ms,
            page_size=request.page_size or 200,
            page_token=request.page_token,
        )
        if self._consumer.recovering:
            raise HistoryReplayUnavailableError("wide aggregation replay changed")
        response = pb.QueryAggregatesResponse(
            next_page_token=page.next_page_token
        )
        for row in page.rows:
            item = response.items.add(
                point_id=row.point_id,
                business_point_id=row.point_id if level is WideDataLevel.BUSINESS else "",
                point_table_id=schema.point_table_id,
                data_level=source.data_level,
                good_count=row.good_count,
                missing_count=row.missing_count,
                result_value_kind=row.result_kind,
            )
            item.window_start.CopyFrom(_date(row.start_us))
            item.window_end.CopyFrom(_date(row.end_us))
            if row.result_kind == 1:
                item.result_i64 = int(cast(int, row.result))
            elif row.result_kind == 2:
                item.result_u64 = int(cast(int, row.result))
            elif row.result_kind == 3:
                item.result = float(cast(float, row.result))
        return response

    @guarded
    async def QueryAggregates(
        self,
        request: pb.QueryAggregatesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QueryAggregatesResponse:
        if request.HasField("wide_filter"):
            if request.HasField("filter"):
                raise QueryRejectedError("cannot mix wide and legacy aggregate filters")
            self._auth.require(
                context, permission="storage.read",
                farm=request.wide_filter.wind_farm_id,
            )
            if self._consumer.recovering:
                raise HistoryReplayUnavailableError("Kafka history replay is not settled")
            return await self._query_wide_aggregates(request)
        self._auth.require(context, permission="storage.read", farm=request.filter.wind_farm_id)
        if self._consumer.recovering:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Storage history is unavailable during Kafka replay reconciliation",
            )
        filter_ = _filter(request.filter)
        if request.aggregation not in (
            pb.STORAGE_AGGREGATION_COUNT,
            pb.STORAGE_AGGREGATION_MIN,
            pb.STORAGE_AGGREGATION_MAX,
            pb.STORAGE_AGGREGATION_AVG,
        ):
            raise QueryRejectedError("aggregation must select count/min/max/avg")
        if not filter_.device_type:
            raise QueryRejectedError("aggregation requires an explicit device_type")
        if not filter_.device_id or not filter_.point_table_id:
            raise QueryRejectedError("aggregation requires one device and one PointTable")
        if request.window_ms == 0 or request.window_ms > 31 * 86400000:
            raise QueryRejectedError("aggregation window must be 1ms..31 days")
        page_size = request.page_size or 200
        if not 1 <= page_size <= self._config.query.max_page_size:
            raise QueryRejectedError("aggregate page_size exceeds configured bound")
        snapshot_id, start_index = self._decode_aggregate_cursor(request, filter_)
        if snapshot_id:
            cached = self._aggregate_snapshots.get(snapshot_id)
            if (
                cached is None
                or cached[0] < time.time()
                or (cached[1] != self._aggregate_fingerprint(request, filter_))
            ):
                raise QueryRejectedError("aggregate snapshot expired or unavailable")
            return self._aggregate_page(
                request, filter_, snapshot_id, cached[2], start_index, page_size
            )
        # This bounded implementation never returns a partial aggregate as complete.
        # If the record budget is exceeded, callers must narrow the time window.
        result: dict[tuple[int, str, str, str, str], list[float]] = {}
        counts: dict[tuple[int, str, str, str, str], tuple[int, int]] = {}
        offset = ""
        scanned = 0
        while True:
            page = await self._query.query(filter_, page_size=1000, page_token=offset)
            for row in page.rows:
                scanned += 1
                if scanned > 10000:
                    await context.abort(
                        grpc.StatusCode.RESOURCE_EXHAUSTED,
                        "narrow the aggregate query window",
                    )
                key = (
                    (row.ts_us // (request.window_ms * 1000)) * (request.window_ms * 1000),
                    row.point_id,
                    row.business_point_id,
                    row.identity[3],
                    row.identity[2],
                )
                valid, missing = counts.get(key, (0, 0))
                if row.value_kind == pb.STORAGE_VALUE_KIND_MISSING:
                    missing += 1
                elif row.quality == "good":
                    if row.value_kind not in (
                        pb.STORAGE_VALUE_KIND_INT64,
                        pb.STORAGE_VALUE_KIND_UINT64,
                        pb.STORAGE_VALUE_KIND_DOUBLE,
                    ):
                        raise QueryRejectedError("aggregate includes a nonnumeric sample")
                    numeric = float(cast(int | float, row.value))
                    if (
                        row.value_kind != pb.STORAGE_VALUE_KIND_DOUBLE
                        and abs(int(cast(int, row.value))) > 2**53
                    ):
                        raise QueryRejectedError("aggregate would lose integer precision")
                    result.setdefault(key, []).append(numeric)
                    valid += 1
                counts[key] = (valid, missing)
            if page.complete:
                break
            offset = page.next_page_token
        ordered = sorted(counts)
        if start_index > len(ordered):
            raise QueryRejectedError("aggregate cursor is beyond available results")
        response = pb.QueryAggregatesResponse()
        for key in ordered:
            begin, point_id, business_id, table_id, _device_id = key
            good_count, missing_count = counts[key]
            item = response.items.add(
                point_id=point_id,
                business_point_id=business_id,
                point_table_id=table_id,
                data_level=request.filter.data_level,
                good_count=good_count,
                missing_count=missing_count,
            )
            item.window_start.CopyFrom(_date(begin))
            item.window_end.CopyFrom(_date(begin + request.window_ms * 1000))
            item.unit = await self._point_tables.get_unit(
                point_table_id=table_id,
                point_id=point_id,
                business_point_id=business_id,
                data_level=filter_.data_level,
            )
            values = result.get(key, [])
            if request.aggregation == pb.STORAGE_AGGREGATION_COUNT:
                item.result = float(good_count)
            elif values:
                import math

                if request.aggregation == pb.STORAGE_AGGREGATION_MIN:
                    item.result = min(values)
                elif request.aggregation == pb.STORAGE_AGGREGATION_MAX:
                    item.result = max(values)
                else:
                    item.result = math.fsum(values) / len(values)
        if len(response.items) <= page_size:
            return response
        # Materialize one bounded immutable result, preventing later Kafka
        # backfill from changing page membership or previously computed sums.
        rows = tuple(item.SerializeToString() for item in response.items)
        snapshot_bytes = sum(map(len, rows))
        if snapshot_bytes > _AGGREGATE_SNAPSHOT_MAX_BYTES:
            raise QueryRejectedError("aggregate snapshot exceeds memory budget")
        now = time.time()
        self._aggregate_snapshots = {
            key: value for key, value in self._aggregate_snapshots.items() if value[0] > now
        }
        cached_bytes = sum(sum(map(len, value[2])) for value in self._aggregate_snapshots.values())
        while self._aggregate_snapshots and (
            len(self._aggregate_snapshots) >= _AGGREGATE_SNAPSHOT_MAX_ENTRIES
            or cached_bytes + snapshot_bytes > _AGGREGATE_SNAPSHOT_MAX_BYTES
        ):
            oldest = min(
                self._aggregate_snapshots, key=lambda key: self._aggregate_snapshots[key][0]
            )
            cached_bytes -= sum(map(len, self._aggregate_snapshots[oldest][2]))
            del self._aggregate_snapshots[oldest]
        snapshot_id = uuid.uuid4().hex
        self._aggregate_snapshots[snapshot_id] = (
            now + 900,
            self._aggregate_fingerprint(request, filter_),
            rows,
        )
        return self._aggregate_page(request, filter_, snapshot_id, rows, 0, page_size)

    def _aggregate_page(
        self,
        request: pb.QueryAggregatesRequest,
        filter_: HistoryFilter,
        snapshot_id: str,
        rows: tuple[bytes, ...],
        index: int,
        page_size: int,
    ) -> pb.QueryAggregatesResponse:
        if index >= len(rows):
            raise QueryRejectedError("aggregate cursor is beyond the snapshot")
        response = pb.QueryAggregatesResponse()
        for row in rows[index : index + page_size]:
            response.items.add().ParseFromString(row)
        if index + page_size < len(rows):
            response.next_page_token = self._encode_aggregate_cursor(
                request, filter_, snapshot_id, index + page_size
            )
        return response

    def _aggregate_fingerprint(
        self, request: pb.QueryAggregatesRequest, filter_: HistoryFilter
    ) -> str:
        payload = json.dumps(
            [filter_.fingerprint(), request.aggregation, request.window_ms],
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _encode_aggregate_cursor(
        self,
        request: pb.QueryAggregatesRequest,
        filter_: HistoryFilter,
        snapshot_id: str,
        index: int,
    ) -> str:
        data = json.dumps(
            [
                self._aggregate_fingerprint(request, filter_),
                snapshot_id,
                index,
                int(time.time()) + 900,
            ],
            separators=(",", ":"),
        ).encode()
        signature = hmac.digest(self._secret, data, "sha256")
        return base64.urlsafe_b64encode(signature + data).decode("ascii")

    def _decode_aggregate_cursor(
        self, request: pb.QueryAggregatesRequest, filter_: HistoryFilter
    ) -> tuple[str, int]:
        if not request.page_token:
            return "", 0
        if len(request.page_token) > 4096:
            raise QueryRejectedError("aggregate cursor exceeds 4 KiB")
        try:
            packed = base64.b64decode(request.page_token.encode(), altchars=b"-_", validate=True)
            signature, raw = packed[:32], packed[32:]
            if len(signature) != 32 or not hmac.compare_digest(
                signature, hmac.digest(self._secret, raw, "sha256")
            ):
                raise QueryRejectedError("aggregate cursor signature invalid")
            identity, snapshot_id, index, expires = json.loads(raw)
            if (
                identity != self._aggregate_fingerprint(request, filter_)
                or not isinstance(snapshot_id, str)
                or len(snapshot_id) != 32
                or type(index) is not int
                or index < 0
                or type(expires) is not int
                or expires < time.time()
            ):
                raise QueryRejectedError("aggregate cursor expired or bound to another request")
            return snapshot_id, index
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise QueryRejectedError("aggregate cursor malformed") from exc

    @guarded
    async def CreateExport(
        self,
        request: pb.CreateExportRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        requested_farm = (
            request.wide_filter.wind_farm_id
            if request.HasField("wide_filter")
            else request.filter.wind_farm_id
        )
        owner = self._auth.require(
            context, permission="storage.export", farm=requested_farm
        )
        if self._consumer.recovering:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Storage history is unavailable during Kafka replay reconciliation",
            )
        if request.HasField("wide_filter"):
            if request.HasField("filter"):
                raise QueryRejectedError(
                    "wide and legacy Storage export filters cannot be combined"
                )
            wide = request.wide_filter
            if not wide.wind_farm_id:
                raise QueryRejectedError("wide export requires one wind farm")
            if wide.page_token:
                raise QueryRejectedError("wide export must begin at a full history horizon")
            levels = {
                pb.STORAGE_DATA_LEVEL_RAW: WideDataLevel.RAW,
                pb.STORAGE_DATA_LEVEL_PROTOCOL: WideDataLevel.PROTOCOL,
                pb.STORAGE_DATA_LEVEL_BUSINESS: WideDataLevel.BUSINESS,
            }
            level = levels.get(wide.data_level)
            if level is None or not wide.HasField("start_time") or not wide.HasField("end_time"):
                raise QueryRejectedError("wide export needs exact level and time bounds")
            identity = WideIdentity(
                wind_farm_id=wide.wind_farm_id,
                device_config_id=wide.device_config_id,
                device_id=wide.device_id,
                data_level=level,
            )
            schema = await self._point_tables.resolve_wide_schema(
                identity, type_mapping=self._config.tdengine.type_mapping,
                max_row_bytes=65536,
            )
            ids = tuple(wide.point_ids) or tuple(
                column.point_id for column in schema.columns
            )
            filter_ = WideHistoryFilter(
                wind_farm_id=identity.wind_farm_id,
                device_id=identity.device_id,
                device_config_id=identity.device_config_id,
                data_level=level,
                start_time_us=_us(wide.start_time),
                end_time_us=_us(wide.end_time),
                point_ids=ids,
                limit=wide.page_size or 200,
                qualities=tuple(wide.qualities),
            )
            record = await self._jobs.create_wide_export(
                request_id=request.request_id,
                filter_=filter_, schema=schema,
                device_type=self._point_tables.wide_device_type(identity),
                export_format=request.export_format, owner_id=owner,
            )
        else:
            filter_ = _filter(request.filter)
            record = await self._jobs.create_export(
                request_id=request.request_id,
                filter_=filter_,
                export_format=request.export_format,
                owner_id=owner,
            )
        return _task(record)

    @guarded
    async def GetExportTask(
        self,
        request: pb.GetStorageTaskRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        owner = self._auth.require(context, permission="storage.export", farm=None)
        record = self._jobs.get_export(request.task_id, owner_id=owner)
        if record is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "export task not visible")
        self._auth.require(context, permission="storage.export", farm=record.wind_farm_id)
        return _task(record)

    @guarded
    async def CreateBackup(
        self,
        request: pb.CreateBackupRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        self._auth.require(context, permission="storage.backup", farm=request.wind_farm_id)
        if self._consumer.recovering:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Storage history is unavailable during Kafka replay reconciliation",
            )
        if not self._config.backup.enabled:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "native TDengine backup is disabled until version-specific verification",
            )
            raise AssertionError("abort must terminate RPC") from None
        record = await self._jobs.create_backup(
            request_id=request.request_id,
            wind_farm_id=request.wind_farm_id,
            owner_id=self._auth.require(
                context, permission="storage.backup", farm=request.wind_farm_id
            ),
        )
        return _task(record)

    @guarded
    async def GetBackupTask(
        self,
        request: pb.GetStorageTaskRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        owner = self._auth.require(context, permission="storage.backup", farm=None)
        record = self._jobs.get_backup(request.task_id, owner_id=owner)
        if record is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "backup task not visible")
        self._auth.require(context, permission="storage.backup", farm=record.wind_farm_id)
        return _task(record)

    @guarded
    async def GetStorageStats(
        self,
        request: pb.GetStorageStatsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.GetStorageStatsResponse:
        self._auth.require(
            context,
            permission="storage.inspect",
            farm=request.wind_farm_id or None,
            global_scope=True,
        )
        counters = self._metrics.snapshot()
        state = "REPLAYING" if self._consumer.recovering else self._config_status()
        response = pb.GetStorageStatsResponse(
            service_state=state,
            persisted_points_total=counters.persisted_points,
            write_unknown_total=counters.write_unknown,
            schema_conflicts_total=counters.schema_conflicts,
            active_queries=self._query.active_queries,
            active_exports=self._jobs.active_exports,
            active_backups=self._jobs.active_backups,
        )
        for item in await self._consumer.snapshot_progress():
            response.partitions.add(
                topic=item[0],
                partition=item[1],
                committed_next_offset=item[2],
                last_confirmed_offset=item[3],
                lag=item[4],
                block_reason=item[5],
            )
        if response.service_state == "READY" and (
            any(item.block_reason for item in response.partitions)
            or self._metadata.list_blocked(page_size=1)
        ):
            response.service_state = "DEGRADED"
        return response

    @guarded
    async def ListBlockedPartitions(
        self,
        request: pb.ListBlockedPartitionsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.ListBlockedPartitionsResponse:
        # Bad JSON may have no reliable farm ID. Only a global admin can inspect it.
        self._auth.require(context, permission="storage.operate", farm=None, global_scope=True)
        size = request.page_size or 100
        if not 1 <= size <= 1000:
            raise QueryRejectedError("blocked partition page_size must be 1..1000")
        after = self._decode_blocked_cursor(request.page_token)
        rows = self._metadata.list_blocked(page_size=size + 1, after=after)
        result = pb.ListBlockedPartitionsResponse()
        for topic, partition, offset, reason in rows[:size]:
            result.items.add(
                topic=topic,
                partition=partition,
                committed_next_offset=offset,
                last_confirmed_offset=offset - 1,
                block_reason=reason,
            )
        if len(rows) > size:
            topic, partition, _, _ = rows[size - 1]
            result.next_page_token = self._encode_blocked_cursor(topic, partition)
        return result

    @guarded
    async def RetryBlockedPartition(
        self,
        request: pb.RetryBlockedPartitionRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.RetryBlockedPartitionResponse:
        self._auth.require(context, permission="storage.operate", farm=None, global_scope=True)
        if not request.request_id or request.partition < 0:
            raise QueryRejectedError("invalid blocked partition retry")
        accepted = await self._consumer.retry_blocked_partition(
            request_id=request.request_id,
            topic=request.topic,
            partition=request.partition,
            expected_offset=request.expected_blocked_offset,
        )
        if not accepted:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "blocked offset did not match expected value",
            )
        return pb.RetryBlockedPartitionResponse(accepted=True, result="RETRY_ACCEPTED")

    def _encode_blocked_cursor(self, topic: str, partition: int) -> str:
        raw = json.dumps(
            [topic, partition, int(time.time()) + 900],
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(hmac.digest(self._secret, raw, "sha256") + raw).decode()

    def _decode_blocked_cursor(self, token: str) -> tuple[str, int] | None:
        if not token:
            return None
        if len(token) > 2048:
            raise QueryRejectedError("blocked partition cursor too long")
        try:
            payload = base64.b64decode(token.encode(), altchars=b"-_", validate=True)
            signature, body = payload[:32], payload[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
                raise QueryRejectedError("blocked partition cursor HMAC invalid")
            item = json.loads(body)
            if (
                not isinstance(item, list)
                or len(item) != 3
                or not isinstance(item[0], str)
                or type(item[1]) is not int
                or type(item[2]) is not int
                or item[2] < time.time()
            ):
                raise QueryRejectedError("blocked partition cursor expired or malformed")
            return item[0], item[1]
        except (ValueError, UnicodeError, TypeError) as exc:
            raise QueryRejectedError("invalid blocked partition cursor") from exc


def build_storage_grpc_server(
    *,
    config: StorageConfig,
    metadata: MetadataStore,
    query: HistoryQueryService,
    wide_query: WideHistoryQueryService,
    wide_aggregation: WideAggregationService,
    jobs: AsyncStorageTasks,
    point_tables: ConfigurationPointTableResolver,
    consumer: KafkaStorageConsumer,
    cursor_secret: bytes,
    config_status: Callable[[], str],
    metrics: StorageRuntimeMetrics,
    config_revisions: Callable[[], tuple[str, str]] | None = None,
) -> grpc.aio.Server:
    """Build but do not start the service; outer runtime manages lifecycle."""
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", 4 * 1024 * 1024),
            ("grpc.max_send_message_length", 4 * 1024 * 1024),
            ("grpc.max_concurrent_streams", config.grpc.max_active_rpcs),
        ],
        maximum_concurrent_rpcs=config.grpc.max_active_rpcs,
    )
    servicer = StorageServicer(
        config=config,
        metadata=metadata,
        query=query,
        wide_query=wide_query,
        wide_aggregation=wide_aggregation,
        jobs=jobs,
        point_tables=point_tables,
        consumer=consumer,
        cursor_secret=cursor_secret,
        config_status=config_status,
        metrics=metrics,
        config_revisions=config_revisions,
    )
    rpc.add_StorageServiceServicer_to_server(servicer, server)
    common_rpc.add_CommonServiceServicer_to_server(servicer, server)
    endpoint = f"{config.grpc.listen_host}:{config.grpc.listen_port}"
    if server.add_insecure_port(endpoint) != config.grpc.listen_port:
        raise RuntimeError("Storage RPC cannot bind its configured loopback endpoint")
    return server
