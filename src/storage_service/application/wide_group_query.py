"""Signed, bounded pagination across immutable device-wide schema groups."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Protocol

from storage_service.application.config import TypeMapping
from storage_service.application.wide_aggregate import WideAggregateRow, WideAggregationService
from storage_service.application.wide_query import (
    WideHistoryFilter,
    WideHistoryQueryService,
    WideHistoryRow,
)
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
from storage_service.domain.wide_schema import WideSchema


class WideGroupedResolver(Protocol):
    """Read-only immutable device versions and their physical schemas."""

    def list_wide_identities(
        self,
        *,
        wind_farm_id: str,
        device_ids: tuple[str, ...],
        point_table_id: str,
        device_type: str,
        data_level: WideDataLevel,
    ) -> tuple[WideIdentity, ...]: ...

    async def resolve_wide_schema(
        self,
        record: WideIdentity,
        *,
        type_mapping: TypeMapping,
        max_row_bytes: int,
    ) -> WideSchema: ...

    def wide_device_type(self, identity: WideIdentity) -> str: ...


@dataclass(frozen=True, slots=True)
class GroupedWideSelector:
    wind_farm_id: str
    device_ids: tuple[str, ...]
    device_type: str
    point_table_id: str
    data_level: WideDataLevel
    point_ids: tuple[str, ...]
    qualities: tuple[int, ...]
    start_time_us: int
    end_time_us: int
    schema_fingerprint: str = ""

    def validate(self) -> None:
        if not self.wind_farm_id or not 0 <= self.start_time_us < self.end_time_us:
            raise ValueError("grouped history requires farm and nonempty time range")
        if len(set(self.device_ids)) != len(self.device_ids) or any(
            not device for device in self.device_ids
        ):
            raise ValueError("grouped history contains duplicate or empty devices")
        if len(set(self.point_ids)) != len(self.point_ids) or any(
            not point for point in self.point_ids
        ):
            raise ValueError("grouped history has duplicate or empty points")
        if len(set(self.qualities)) != len(self.qualities) or any(
            type(quality) is not int or not 0 <= quality <= 3
            for quality in self.qualities
        ):
            raise ValueError("grouped history contains invalid quality values")


@dataclass(frozen=True, slots=True)
class GroupedWideRows:
    identity: WideIdentity
    device_type: str
    schema: WideSchema
    point_ids: tuple[str, ...]
    rows: tuple[WideHistoryRow, ...]


@dataclass(frozen=True, slots=True)
class GroupedWidePage:
    groups: tuple[GroupedWideRows, ...]
    next_page_token: str
    complete: bool


@dataclass(frozen=True, slots=True)
class GroupedWideAggregate:
    identity: WideIdentity
    device_type: str
    schema: WideSchema
    rows: tuple[WideAggregateRow, ...]


@dataclass(frozen=True, slots=True)
class GroupedWideAggregatePage:
    groups: tuple[GroupedWideAggregate, ...]
    next_page_token: str
    complete: bool


class WideGroupedHistoryService:
    """Read one physical family per page without merging independent schemas."""

    def __init__(
        self,
        *,
        history: WideHistoryQueryService,
        aggregation: WideAggregationService,
        resolver: WideGroupedResolver,
        type_mapping: TypeMapping,
        cursor_secret: bytes,
        max_page_size: int = 1000,
        max_groups: int = 1000,
        max_row_bytes: int = 65536,
    ) -> None:
        if (
            len(cursor_secret) < 32
            or not 1 <= max_page_size <= 1000
            or not 1 <= max_groups <= 10000
            or max_row_bytes < 1024
        ):
            raise ValueError("grouped history requires bounded resources and cursor key")
        self._history = history
        self._aggregation = aggregation
        self._resolver = resolver
        self._type_mapping = type_mapping
        self._key = cursor_secret
        self._max_page_size = max_page_size
        self._max_groups = max_groups
        self._max_row_bytes = max_row_bytes

    def _encode_cursor(
        self, *, digest: str, index: int, inner: str, horizon: int
    ) -> str:
        payload = json.dumps(
            [digest, index, inner, horizon, int(time.time()) + 900],
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(
            hmac.digest(self._key, payload, "sha256") + payload
        ).decode("ascii")

    def _decode_cursor(
        self, token: str, *, digest: str, total: int, selector: GroupedWideSelector
    ) -> tuple[int, str, int]:
        if not token or len(token) > 8192:
            raise ValueError("invalid grouped history cursor")
        try:
            data = base64.b64decode(token, altchars=b"-_", validate=True)
            signature, payload = data[:32], data[32:]
            if len(signature) != 32 or not hmac.compare_digest(
                signature, hmac.digest(self._key, payload, "sha256")
            ):
                raise ValueError("grouped history cursor signature mismatch")
            stored, index, inner, horizon, expiry = json.loads(payload)
            if (
                stored != digest
                or type(index) is not int
                or not 0 <= index < total
                or not isinstance(inner, str)
                or type(horizon) is not int
                or not selector.start_time_us < horizon <= selector.end_time_us
                or type(expiry) is not int
                or expiry < int(time.time())
            ):
                raise ValueError("grouped history cursor expired or rebound")
            return index, inner, horizon
        except (TypeError, ValueError, UnicodeError, IndexError) as exc:
            raise ValueError("malformed grouped history cursor") from exc

    async def _resolve_groups(
        self,
        selector: GroupedWideSelector,
        *,
        page_size: int,
        kind: str,
        window_ms: int,
    ) -> tuple[list[tuple[WideIdentity, WideSchema, str]], str]:
        """Resolve the immutable physical families and bind pagination to them."""
        selector.validate()
        identities = self._resolver.list_wide_identities(
            wind_farm_id=selector.wind_farm_id,
            device_ids=selector.device_ids,
            point_table_id=selector.point_table_id,
            device_type=selector.device_type,
            data_level=selector.data_level,
        )
        resolved: list[tuple[WideIdentity, WideSchema, str]] = []
        physical_keys: set[tuple[str, str, str]] = set()
        for identity in identities:
            schema = await self._resolver.resolve_wide_schema(
                identity,
                type_mapping=self._type_mapping,
                max_row_bytes=self._max_row_bytes,
            )
            if selector.schema_fingerprint and schema.fingerprint != selector.schema_fingerprint:
                continue
            columns = {column.point_id for column in schema.columns}
            if selector.point_ids and not set(selector.point_ids).issubset(columns):
                raise ValueError("selected point is absent from an immutable schema")
            key = (schema.fingerprint, identity.device_id, schema.point_table_id)
            if key in physical_keys:
                continue
            physical_keys.add(key)
            resolved.append((identity, schema, self._resolver.wide_device_type(identity)))
            if len(resolved) > self._max_groups:
                raise ValueError("grouped history exceeds configured schema group limit")
        resolved.sort(key=lambda group: (group[1].fingerprint, group[0].device_id))
        identity_data = (
            selector.wind_farm_id,
            selector.device_ids,
            selector.device_type,
            selector.point_table_id,
            selector.data_level.value,
            selector.point_ids,
            selector.qualities,
            selector.start_time_us,
            selector.end_time_us,
            selector.schema_fingerprint,
            page_size,
            kind,
            window_ms,
            tuple(
                (identity.device_id, identity.device_config_id, schema.fingerprint)
                for identity, schema, _device_type in resolved
            ),
        )
        digest = hashlib.sha256(
            json.dumps(identity_data, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return resolved, digest
    async def query(
        self,
        *,
        selector: GroupedWideSelector,
        page_size: int,
        page_token: str = "",
    ) -> GroupedWidePage:
        selector.validate()
        if type(page_size) is not int or not 1 <= page_size <= self._max_page_size:
            raise ValueError("grouped history page size exceeds limit")
        resolved, digest = await self._resolve_groups(
            selector, page_size=page_size, kind="query", window_ms=0
        )
        if not resolved:
            if page_token:
                raise ValueError("grouped history cursor references unavailable schemas")
            return GroupedWidePage((), "", True)
        index = 0
        inner = ""
        horizon = min(selector.end_time_us, time.time_ns() // 1000)
        if page_token:
            index, inner, horizon = self._decode_cursor(
                page_token, digest=digest, total=len(resolved), selector=selector
            )
        if horizon <= selector.start_time_us:
            return GroupedWidePage((), "", True)

        # One physical family per response keeps pagination and memory bounded.
        # Empty families are skipped without synthesizing timestamped rows.
        while index < len(resolved):
            identity, schema, device_type = resolved[index]
            points = selector.point_ids or tuple(column.point_id for column in schema.columns)
            # Reserve fourfold wire overhead for typed values and descriptors.
            physical_page_limit = max(1, (2 * 1024 * 1024) // (4 * schema.declared_row_bytes))
            filter_ = WideHistoryFilter(
                wind_farm_id=identity.wind_farm_id,
                device_id=identity.device_id,
                device_config_id=identity.device_config_id,
                data_level=selector.data_level,
                start_time_us=selector.start_time_us,
                end_time_us=horizon,
                point_ids=points,
                qualities=selector.qualities,
                limit=min(page_size, physical_page_limit),
            )
            page = await self._history.query(
                schema=schema,
                filter_=filter_,
                device_type=device_type,
                page_token=inner,
            )
            next_index = index if not page.complete else index + 1
            next_inner = page.next_page_token if not page.complete else ""
            finished = next_index >= len(resolved)
            token = (
                ""
                if finished
                else self._encode_cursor(
                    digest=digest,
                    index=next_index,
                    inner=next_inner,
                    horizon=horizon,
                )
            )
            if page.rows or not page.complete:
                group = GroupedWideRows(identity, device_type, schema, points, page.rows)
                return GroupedWidePage((group,), token, finished)
            index += 1
            inner = ""
        return GroupedWidePage((), "", True)
