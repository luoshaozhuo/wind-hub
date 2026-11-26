"""StorageService gRPC handlers: typed Contracts RPC and scoped token authority."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import math
import os
import time
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
from storage_service.application.ingestion_contract import ConfigHistoryUnavailableError
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.tasks import AsyncStorageTasks, ExportQuotaExceededError
from storage_service.application.wide_aggregate import WideAggregationService
from storage_service.application.wide_group_query import GroupedWideSelector, WideGroupedHistoryService
from storage_service.application.wide_query import WideHistoryFilter, WideHistoryQueryService
from storage_service.domain.errors import (
    HistoryReplayUnavailableError,
    QueryRejectedError,
    TDengineSchemaConflictError,
)
from storage_service.domain.tasks import TaskRecord
from storage_service.domain.quality_bitmap import pack_quality_bitmap
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
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

T = TypeVar("T")
_LOG = logging.getLogger(__name__)


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


def _wide_selector(source: pb.StorageFilter) -> GroupedWideSelector:
    """Convert the frozen v2 filter without accepting legacy narrow fields."""
    levels = {
        pb.STORAGE_DATA_LEVEL_RAW: WideDataLevel.RAW,
        pb.STORAGE_DATA_LEVEL_PROTOCOL: WideDataLevel.PROTOCOL,
        pb.STORAGE_DATA_LEVEL_BUSINESS: WideDataLevel.BUSINESS,
    }
    level = levels.get(source.data_level)
    if (
        level is None
        or not source.HasField("start_time")
        or not source.HasField("end_time")
    ):
        raise QueryRejectedError("Storage query requires level and UTC time range")
    return GroupedWideSelector(
        wind_farm_id=source.wind_farm_id,
        device_ids=tuple(source.device_ids),
        device_type=source.device_type,
        point_table_id=source.point_table_id,
        data_level=level,
        point_ids=tuple(source.point_ids),
        qualities=tuple(int(quality) for quality in source.qualities),
        start_time_us=_us(source.start_time),
        end_time_us=_us(source.end_time),
        schema_fingerprint=(
            source.schema_fingerprint if source.HasField("schema_fingerprint") else ""
        ),
    )


def _set_wide_value(target: pb.StorageValue, value: object, sql_type: str) -> None:
    """Bind one physical value without changing signedness or NULL presence."""
    if value is None:
        return
    if sql_type == "BOOL":
        if type(value) is not bool:
            raise TDengineSchemaConflictError("wide BOOL value is invalid")
        target.value_bool = value
    elif sql_type in {"TINYINT", "SMALLINT", "INT", "BIGINT"}:
        bits = {"TINYINT": 8, "SMALLINT": 16, "INT": 32, "BIGINT": 64}[sql_type]
        if type(value) is not int or not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            raise TDengineSchemaConflictError("wide signed integer is outside its type")
        target.value_i64 = value
    elif sql_type.endswith(" UNSIGNED"):
        bits = {
            "TINYINT UNSIGNED": 8,
            "SMALLINT UNSIGNED": 16,
            "INT UNSIGNED": 32,
            "BIGINT UNSIGNED": 64,
        }.get(sql_type)
        if bits is None or type(value) is not int or not 0 <= value < (1 << bits):
            raise TDengineSchemaConflictError("wide unsigned integer is outside its type")
        target.value_u64 = value
    elif sql_type in {"FLOAT", "DOUBLE"}:
        if type(value) not in (int, float) or (
            type(value) is int and abs(value) > 2**53
        ):
            raise TDengineSchemaConflictError("wide floating-point value is invalid")
        try:
            numeric = float(value)
        except OverflowError as exc:
            raise TDengineSchemaConflictError("wide floating-point value overflow") from exc
        if not math.isfinite(numeric) or (
            sql_type == "FLOAT" and abs(numeric) > 3.4028234663852886e38
        ):
            raise TDengineSchemaConflictError("wide floating-point value out of range")
        target.value_f64 = numeric
    elif sql_type.startswith("VARCHAR("):
        width = int(sql_type.removeprefix("VARCHAR(").removesuffix(")"))
        if type(value) is not str or len(value.encode("utf-8")) > width:
            raise TDengineSchemaConflictError("wide text is outside its declared width")
        target.value_text = value
    elif sql_type.startswith("VARBINARY("):
        width = int(sql_type.removeprefix("VARBINARY(").removesuffix(")"))
        if not isinstance(value, bytes | bytearray | memoryview) or len(value) > width:
            raise TDengineSchemaConflictError("wide binary value is invalid")
        target.payload_bytes = bytes(value)
    else:
        raise TDengineSchemaConflictError("unsupported physical wide column type")


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
        wide_query: WideHistoryQueryService,
        wide_aggregation: WideAggregationService,
        grouped_query: WideGroupedHistoryService,
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
        self._wide_query = wide_query
        self._grouped_query = grouped_query
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
    async def QuerySamples(
        self,
        request: pb.QuerySamplesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QuerySamplesResponse:
        """Return pinned device-wide schema groups without legacy narrow samples."""
        if not request.HasField("filter"):
            raise QueryRejectedError("Storage query filter is required")
        source = request.filter
        self._auth.require(context, permission="storage.read", farm=source.wind_farm_id)
        if self._consumer.recovering:
            raise HistoryReplayUnavailableError("Storage Kafka replay is not settled")
        selector = _wide_selector(source)
        level = selector.data_level
        page = await self._grouped_query.query(
            selector=selector,
            page_size=request.page_size or 200,
            page_token=request.page_token,
        )
        if self._consumer.recovering:
            raise HistoryReplayUnavailableError("Storage Kafka replay began during query")
        response = pb.QuerySamplesResponse(
            next_page_token=page.next_page_token, complete=page.complete
        )
        for family in page.groups:
            group = response.groups.add(
                wind_farm_id=family.identity.wind_farm_id,
                device_id=family.identity.device_id,
                device_type=family.device_type,
                point_table_id=family.schema.point_table_id,
                data_level=source.data_level,
                schema_fingerprint=family.schema.fingerprint,
            )
            columns = {column.point_id: column for column in family.schema.columns}
            for point_id in family.point_ids:
                column = columns[point_id]
                source_id, business_id, native_type, unit = (
                    await self._point_tables.get_wide_column_metadata(
                        point_table_id=family.schema.point_table_id,
                        physical_point_id=point_id,
                        data_level=level,
                    )
                )
                group.columns.add(
                    point_id=source_id,
                    business_point_id=business_id,
                    unit=unit,
                    data_type=native_type,
                    physical_name=column.column_name,
                )
            for row in family.rows:
                if len(row.points) != len(family.point_ids):
                    raise TDengineSchemaConflictError("incomplete physical row")
                result = group.rows.add()
                result.sample_time.CopyFrom(_date(row.sample_time_us))
                qualities: list[int] = []
                for expected, (point_id, value, quality) in zip(
                    family.point_ids, row.points, strict=True
                ):
                    if point_id != expected:
                        raise TDengineSchemaConflictError("wide point ordering mismatch")
                    qualities.append(int(quality))
                    typed = result.values.add()
                    _set_wide_value(typed, value, columns[point_id].sql_type)
                result.quality = pack_quality_bitmap(qualities)
        if response.ByteSize() > 4 * 1024 * 1024:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "Storage grouped query response exceeds 4 MiB",
            )
            raise AssertionError("abort must terminate RPC") from None
        return response

    @guarded
    async def QueryAggregates(
        self,
        request: pb.QueryAggregatesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QueryAggregatesResponse:
        """Aggregate each pinned version independently with explicit Quality counts."""
        if not request.HasField("filter"):
            raise QueryRejectedError("Storage aggregation filter is required")
        source = request.filter
        self._auth.require(context, permission="storage.read", farm=source.wind_farm_id)
        if self._consumer.recovering:
            raise HistoryReplayUnavailableError("Storage Kafka replay is not settled")
        selector = _wide_selector(source)
        operations = {
            pb.STORAGE_AGGREGATION_COUNT: "count",
            pb.STORAGE_AGGREGATION_MIN: "min",
            pb.STORAGE_AGGREGATION_MAX: "max",
            pb.STORAGE_AGGREGATION_AVG: "avg",
        }
        operation = operations.get(request.aggregation)
        if operation is None:
            raise QueryRejectedError("Storage aggregate requires count/min/max/avg")
        page = await self._grouped_query.aggregate(
            selector=selector,
            operation=operation,
            window_ms=request.window_ms,
            page_size=request.page_size or 200,
            page_token=request.page_token,
        )
        if self._consumer.recovering:
            raise HistoryReplayUnavailableError("Storage replay began during aggregation")
        response = pb.QueryAggregatesResponse(next_page_token=page.next_page_token)
        for family in page.groups:
            physical = {column.point_id: column for column in family.schema.columns}
            for row in family.rows:
                column = physical[row.point_id]
                source_id, business_id, native_type, unit = (
                    await self._point_tables.get_wide_column_metadata(
                        point_table_id=family.schema.point_table_id,
                        physical_point_id=row.point_id,
                        data_level=selector.data_level,
                    )
                )
                item = response.items.add(
                    wind_farm_id=family.identity.wind_farm_id,
                    device_id=family.identity.device_id,
                    point_table_id=family.schema.point_table_id,
                    data_level=source.data_level,
                    schema_fingerprint=family.schema.fingerprint,
                    good_count=row.good_count,
                    missing_count=row.missing_count,
                    valid_count=row.valid_count,
                )
                item.window_start.CopyFrom(_date(row.start_us))
                item.window_end.CopyFrom(_date(row.end_us))
                item.column.CopyFrom(
                    pb.StorageColumn(
                        point_id=source_id,
                        business_point_id=business_id,
                        unit=unit,
                        data_type=native_type,
                        physical_name=column.column_name,
                    )
                )
                if row.result_kind == 1:
                    item.result.value_i64 = int(cast(int, row.result))
                elif row.result_kind == 2:
                    item.result.value_u64 = int(cast(int, row.result))
                elif row.result_kind == 3:
                    item.result.value_f64 = float(cast(float, row.result))
        if response.ByteSize() > 4 * 1024 * 1024:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "Storage grouped aggregates exceed 4 MiB",
            )
            raise AssertionError("abort must terminate RPC") from None
        return response

    @guarded
    async def CreateExport(
        self,
        request: pb.CreateExportRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        """Create one idempotent export spanning pinned physical schema families."""
        if not request.HasField("filter"):
            raise QueryRejectedError("Storage export filter is required")
        selector = _wide_selector(request.filter)
        owner = self._auth.require(
            context, permission="storage.export", farm=selector.wind_farm_id
        )
        if self._consumer.recovering:
            raise HistoryReplayUnavailableError("Storage history is unavailable during replay")
        record = await self._jobs.create_grouped_export(
            request_id=request.request_id,
            selector=selector,
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
            active_queries=self._wide_query.active_queries,
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
