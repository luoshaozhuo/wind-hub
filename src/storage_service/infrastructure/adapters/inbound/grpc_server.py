"""StorageService gRPC handlers: typed Contracts RPC and scoped token authority."""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from datetime import UTC, datetime, timedelta
from functools import wraps
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb
from contracts import storage_service_pb2_grpc as rpc
from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import ConfigHistoryUnavailable
from storage_service.application.query import (
    HistoryFilter,
    HistoryQueryService,
    QueryRejected,
)
from storage_service.application.tasks import AsyncStorageTasks, ExportQuotaExceeded
from storage_service.domain.sample import DataLevel
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
)
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import (
    MetadataConflict,
    MetadataStore,
    TaskRecord,
)
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflict,
)

T = TypeVar("T")


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
        raise QueryRejected("timestamp exceeds TDengine microsecond precision")
    return timestamp.seconds * 1_000_000 + timestamp.nanos // 1000


def _filter(source: pb.StorageFilter) -> HistoryFilter:
    levels = {
        pb.STORAGE_DATA_LEVEL_RAW: DataLevel.RAW,
        pb.STORAGE_DATA_LEVEL_PROTOCOL: DataLevel.PROTOCOL,
        pb.STORAGE_DATA_LEVEL_BUSINESS: DataLevel.BUSINESS,
    }
    if source.data_level not in levels:
        raise QueryRejected("query must select exactly one explicit data_level")
    if not source.HasField("start_time") or not source.HasField("end_time"):
        raise QueryRejected("both query timestamps are mandatory")
    return HistoryFilter(
        wind_farm_id=source.wind_farm_id,
        data_level=levels[source.data_level],
        start_us=_us(source.start_time), end_us=_us(source.end_time),
        device_type=source.device_type, device_id=source.device_id,
        point_table_id=source.point_table_id, point_id=source.point_id,
        business_point_id=source.business_point_id,
        qualities=tuple(source.qualities),
    )


class AuthorizationFailure(PermissionError):
    """Missing/unauthorized operator token for this farm and action."""


class AuthorizationPolicy:
    """Every client must authenticate; no insecure anonymous/mTLS fallback."""

    def __init__(self, config: StorageConfig) -> None:
        self._callers = config.auth.callers

    def require(
        self, context: grpc.aio.ServicerContext[Any, Any],
        *, permission: str, farm: str | None, global_scope: bool = False,
    ) -> str:
        headers = dict(context.invocation_metadata())
        header = headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise AuthorizationFailure("missing bearer authentication")
        candidate = header[7:]
        if not candidate or len(candidate) > 4096:
            raise AuthorizationFailure("invalid bearer token")
        for caller in self._callers:
            token = os.environ.get(caller.token_env)
            if not token or not hmac.compare_digest(candidate, token):
                continue
            authorized = (
                permission in caller.permissions
                or "storage.admin" in caller.permissions
            )
            if not authorized:
                raise AuthorizationFailure("missing Storage permission")
            if farm is None:
                if global_scope and "storage.admin" not in caller.permissions:
                    raise AuthorizationFailure("global operation requires Storage admin")
            elif farm not in caller.wind_farms and not (
                "*" in caller.wind_farms and "storage.admin" in caller.permissions
            ):
                raise AuthorizationFailure("Storage farm scope does not match")
            return caller.token_env
        raise AuthorizationFailure("invalid bearer token")


def guarded(operation: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
    """Fail closed with stable gRPC statuses, hiding internal SQL and file paths."""

    @wraps(operation)
    async def call(
        self: StorageServicer, request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> T:
        try:
            return await operation(self, request, context)
        except grpc.RpcError:
            raise
        except AuthorizationFailure:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "Storage authorization denied")
            raise AssertionError("abort must terminate RPC")
        except (QueryRejected, ValueError):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid Storage request")
            raise AssertionError("abort must terminate RPC")
        except MetadataConflict:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "Storage idempotency conflict")
            raise AssertionError("abort must terminate RPC")
        except (ConfigHistoryUnavailable, TDengineSchemaConflict):
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "historical configuration or storage schema is unavailable",
            )
            raise AssertionError("abort must terminate RPC")
        except ExportQuotaExceeded:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Storage quota exceeded")
            raise AssertionError("abort must terminate RPC")
        except (OSError, TimeoutError, ConnectionError):
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Storage dependency unavailable")
            raise AssertionError("abort must terminate RPC")
        except RuntimeError:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Storage operation not ready")
            raise AssertionError("abort must terminate RPC")

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


class StorageServicer(rpc.StorageServiceServicer):
    """Storage transport delegates all persistence and semantic policy to services."""

    def __init__(
        self, *, config: StorageConfig, metadata: MetadataStore,
        query: HistoryQueryService, jobs: AsyncStorageTasks,
        point_tables: ConfigurationPointTableResolver,
        consumer: KafkaStorageConsumer,
        cursor_secret: bytes,
    ) -> None:
        self._auth = AuthorizationPolicy(config)
        self._config = config
        self._metadata = metadata
        self._query = query
        self._jobs = jobs
        self._point_tables = point_tables
        self._consumer = consumer
        if len(cursor_secret) < 32:
            raise ValueError("Storage cursor secret needs 256 bits")
        self._secret = cursor_secret

    @guarded
    async def QuerySamples(
        self, request: pb.QuerySamplesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QuerySamplesResponse:
        filter_ = _filter(request.filter)
        self._auth.require(context, permission="storage.read", farm=filter_.wind_farm_id)
        page = await self._query.query(
            filter_,
            page_size=request.page_size or 200, page_token=request.page_token,
        )
        reply = pb.QuerySamplesResponse(
            next_page_token=page.next_page_token, complete=page.complete
        )
        for row in page.rows:
            farm, device_type, device_id, table_id, level, _ = row.identity
            if farm != filter_.wind_farm_id or level != filter_.data_level.value:
                raise TDengineSchemaConflict("history identity disagrees with query")
            unit = await self._point_tables.get_unit(
                point_table_id=table_id, point_id=row.point_id,
                business_point_id=row.business_point_id,
                data_level=filter_.data_level,
            )
            sample = reply.items.add(
                wind_farm_id=farm, device_type=device_type,
                device_id=device_id, point_table_id=table_id,
                point_id=row.point_id, business_point_id=row.business_point_id,
                sample_id=row.sample_id, data_level=request.filter.data_level,
                value_kind=row.value_kind, quality=row.quality, unit=unit,
                logical_key=row.logical_key, content_sha256=row.content_sha256,
            )
            sample.sample_time.CopyFrom(_date(row.ts_us))
            sample.publish_time.CopyFrom(_date(row.publish_time_us))
            if row.receive_time_us is not None:
                sample.receive_time.CopyFrom(_date(row.receive_time_us))
            if row.value_kind == pb.STORAGE_VALUE_KIND_INT64:
                sample.value_i64 = int(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_UINT64:
                sample.value_u64 = int(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_DOUBLE:
                sample.value_f64 = float(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_BOOL:
                sample.value_bool = bool(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_TEXT:
                sample.value_text = str(row.value)
            elif row.value_kind == pb.STORAGE_VALUE_KIND_BYTES:
                sample.payload_bytes = bytes(row.value)
            elif row.value_kind != pb.STORAGE_VALUE_KIND_MISSING:
                raise TDengineSchemaConflict("unknown stored value kind")
        return reply

    @guarded
    async def QueryAggregates(
        self, request: pb.QueryAggregatesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.QueryAggregatesResponse:
        filter_ = _filter(request.filter)
        self._auth.require(context, permission="storage.read", farm=filter_.wind_farm_id)
        if request.aggregation not in (
            pb.STORAGE_AGGREGATION_COUNT, pb.STORAGE_AGGREGATION_MIN,
            pb.STORAGE_AGGREGATION_MAX, pb.STORAGE_AGGREGATION_AVG,
        ):
            raise QueryRejected("aggregation must select count/min/max/avg")
        if not filter_.device_id or not filter_.point_table_id:
            raise QueryRejected("aggregation requires one device and one PointTable")
        if request.window_ms == 0 or request.window_ms > 31 * 86400000:
            raise QueryRejected("aggregation window must be 1ms..31 days")
        if request.page_token:
            # A native server-side aggregate cursor is required for giant histories.
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "aggregate cursor not enabled")
            raise AssertionError("abort must terminate RPC")
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
                    row.point_id, row.business_point_id, row.identity[3],
                    row.identity[2],
                )
                valid, missing = counts.get(key, (0, 0))
                if row.value_kind == pb.STORAGE_VALUE_KIND_MISSING:
                    missing += 1
                elif row.quality == "good":
                    if row.value_kind not in (
                        pb.STORAGE_VALUE_KIND_INT64, pb.STORAGE_VALUE_KIND_UINT64,
                        pb.STORAGE_VALUE_KIND_DOUBLE,
                    ):
                        raise QueryRejected("aggregate includes a nonnumeric sample")
                    numeric = float(row.value)
                    if row.value_kind != pb.STORAGE_VALUE_KIND_DOUBLE and abs(
                        int(row.value)
                    ) > 2**53:
                        raise QueryRejected("aggregate would lose integer precision")
                    result.setdefault(key, []).append(numeric)
                    valid += 1
                counts[key] = (valid, missing)
            if page.complete:
                break
            offset = page.next_page_token
        if len(counts) > (request.page_size or 200):
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, "aggregate result exceeds one page"
            )
        response = pb.QueryAggregatesResponse()
        for key in sorted(counts):
            begin, point_id, business_id, table_id, _device_id = key
            good_count, missing_count = counts[key]
            item = response.items.add(
                point_id=point_id, business_point_id=business_id,
                point_table_id=table_id,
                data_level=request.filter.data_level,
                good_count=good_count, missing_count=missing_count,
            )
            item.window_start.CopyFrom(_date(begin))
            item.window_end.CopyFrom(_date(begin + request.window_ms * 1000))
            item.unit = await self._point_tables.get_unit(
                point_table_id=table_id, point_id=point_id,
                business_point_id=business_id, data_level=filter_.data_level,
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
        return response

    @guarded
    async def CreateExport(
        self, request: pb.CreateExportRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        filter_ = _filter(request.filter)
        owner = self._auth.require(
            context, permission="storage.export", farm=filter_.wind_farm_id
        )
        record = await self._jobs.create_export(
            request_id=request.request_id, filter_=filter_,
            export_format=request.export_format, owner_id=owner,
        )
        return _task(record)

    @guarded
    async def GetExportTask(
        self, request: pb.GetStorageTaskRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        owner = self._auth.require(context, permission="storage.export", farm=None)
        record = self._jobs.get_export(request.task_id, owner_id=owner)
        if record is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "export task not visible")
        return _task(record)

    @guarded
    async def CreateBackup(
        self, request: pb.CreateBackupRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        self._auth.require(
            context, permission="storage.backup", farm=request.wind_farm_id
        )
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "TDengine native backup requires verified target-version taosdump support",
        )
        raise AssertionError("abort must terminate RPC")

    @guarded
    async def GetBackupTask(
        self, request: pb.GetStorageTaskRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.StorageTask:
        owner = self._auth.require(context, permission="storage.backup", farm=None)
        record = self._metadata.get_task(request.task_id, kind="BACKUP", owner_id=owner)
        if record is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "backup task not visible")
        return _task(record)

    @guarded
    async def GetStorageStats(
        self, request: pb.GetStorageStatsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.GetStorageStatsResponse:
        self._auth.require(
            context, permission="storage.inspect",
            farm=request.wind_farm_id or None,
            global_scope=True,
        )
        response = pb.GetStorageStatsResponse(service_state="READY")
        for item in await self._consumer.snapshot_progress():
            response.partitions.add(
                topic=item[0], partition=item[1],
                committed_next_offset=item[2], last_confirmed_offset=item[3],
                lag=item[4], block_reason=item[5],
            )
        if any(item.block_reason for item in response.partitions):
            response.service_state = "DEGRADED"
        return response

    @guarded
    async def ListBlockedPartitions(
        self, request: pb.ListBlockedPartitionsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.ListBlockedPartitionsResponse:
        # Bad JSON may have no reliable farm ID. Only a global admin can inspect it.
        self._auth.require(context, permission="storage.operate", farm=None, global_scope=True)
        size = request.page_size or 100
        if not 1 <= size <= 1000:
            raise QueryRejected("blocked partition page_size must be 1..1000")
        after = self._decode_blocked_cursor(request.page_token)
        rows = self._metadata.list_blocked(page_size=size + 1, after=after)
        result = pb.ListBlockedPartitionsResponse()
        for topic, partition, offset, reason in rows[:size]:
            result.items.add(
                topic=topic, partition=partition,
                committed_next_offset=offset, last_confirmed_offset=offset - 1,
                block_reason=reason,
            )
        if len(rows) > size:
            topic, partition, _, _ = rows[size - 1]
            result.next_page_token = self._encode_blocked_cursor(topic, partition)
        return result

    @guarded
    async def RetryBlockedPartition(
        self, request: pb.RetryBlockedPartitionRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.RetryBlockedPartitionResponse:
        self._auth.require(context, permission="storage.operate", farm=None)
        if not request.request_id or request.partition < 0:
            raise QueryRejected("invalid blocked partition retry")
        accepted = await self._consumer.retry_blocked_partition(
            request_id=request.request_id, topic=request.topic,
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
        return base64.urlsafe_b64encode(
            hmac.digest(self._secret, raw, "sha256") + raw
        ).decode()

    def _decode_blocked_cursor(self, token: str) -> tuple[str, int] | None:
        if not token:
            return None
        if len(token) > 2048:
            raise QueryRejected("blocked partition cursor too long")
        try:
            payload = base64.b64decode(token.encode(), altchars=b"-_", validate=True)
            signature, body = payload[:32], payload[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
                raise QueryRejected("blocked partition cursor HMAC invalid")
            item = json.loads(body)
            if (
                not isinstance(item, list) or len(item) != 3
                or not isinstance(item[0], str) or type(item[1]) is not int
                or type(item[2]) is not int or item[2] < time.time()
            ):
                raise QueryRejected("blocked partition cursor expired or malformed")
            return item[0], item[1]
        except (ValueError, UnicodeError, TypeError) as exc:
            raise QueryRejected("invalid blocked partition cursor") from exc


def build_storage_grpc_server(
    *, config: StorageConfig, metadata: MetadataStore,
    query: HistoryQueryService, jobs: AsyncStorageTasks,
    point_tables: ConfigurationPointTableResolver,
    consumer: KafkaStorageConsumer, cursor_secret: bytes,
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
    rpc.add_StorageServiceServicer_to_server(
        StorageServicer(
            config=config, metadata=metadata, query=query, jobs=jobs,
            point_tables=point_tables, consumer=consumer,
            cursor_secret=cursor_secret,
        ),
        server,
    )
    endpoint = f"{config.grpc.listen_host}:{config.grpc.listen_port}"
    if server.add_insecure_port(endpoint) != config.grpc.listen_port:
        raise RuntimeError("Storage RPC cannot bind its configured loopback endpoint")
    return server
