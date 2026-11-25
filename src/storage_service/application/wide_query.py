"""Device-wide history pagination with scoped, signed keyset cursors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import WideSchema


@dataclass(frozen=True, slots=True)
class WideHistoryFilter:
    wind_farm_id: str
    device_id: str
    device_config_id: str
    data_level: WideDataLevel
    start_time_us: int
    end_time_us: int
    point_ids: tuple[str, ...]
    limit: int
    after_time_us: int | None = None
    qualities: tuple[int, ...] = ()

    def validate(self, schema: WideSchema, *, maximum: int) -> None:
        if (
            not self.wind_farm_id or not self.device_id or not self.device_config_id
            or self.data_level != schema.data_level
        ):
            raise ValueError("wide query requires an exact device version and data level")
        if (
            type(self.start_time_us) is not int or type(self.end_time_us) is not int
            or self.start_time_us >= self.end_time_us
        ):
            raise ValueError("wide query time range must be nonempty")
        if type(self.limit) is not int or not 1 <= self.limit <= maximum:
            raise ValueError("wide query limit exceeds configured maximum")
        if (
            self.after_time_us is not None
            and (type(self.after_time_us) is not int or self.after_time_us < self.start_time_us)
        ):
            raise ValueError("wide query pagination cursor is invalid")
        if self.qualities:
            if len(self.point_ids) != 1:
                raise ValueError(
                    "quality filtering requires exactly one selected physical point"
                )
            if (
                len(set(self.qualities)) != len(self.qualities)
                or any(type(q) is not int or not 0 <= q <= 3 for q in self.qualities)
            ):
                raise ValueError("quality filter contains invalid two-bit states")
        actual = {column.point_id for column in schema.columns}
        if not self.point_ids or len(set(self.point_ids)) != len(self.point_ids):
            raise ValueError("wide query field set must be unique and nonempty")
        if not set(self.point_ids).issubset(actual):
            raise ValueError("wide query selects fields outside the exact physical schema")


@dataclass(frozen=True, slots=True)
class WideHistoryRow:
    sample_time_us: int
    points: tuple[tuple[str, object, PointQuality], ...]

@dataclass(frozen=True, slots=True)
class WideHistoryPage:
    rows: tuple[WideHistoryRow, ...]
    next_page_token: str
    complete: bool
    schema_fingerprint: str


class WideQueryBackend(Protocol):
    async def verify_existing_wide_table(
        self, schema: WideSchema, *,
        wind_farm_id: str, device_id: str, device_type: str,
    ) -> None: ...

    async def fetch_wide_history(
        self, schema: WideSchema, query: WideHistoryFilter,
    ) -> Sequence[WideHistoryRow]: ...


class WideCursorCodec:
    """Version and filter-bound, 15-minute HMAC pagination token."""

    def __init__(self, secret: bytes, *, ttl_seconds: int = 900) -> None:
        if len(secret) < 32 or not 1 <= ttl_seconds <= 3600:
            raise ValueError("wide pagination requires a strong key and bounded TTL")
        self._key = secret
        self._ttl = ttl_seconds

    @staticmethod
    def _identity(filter_: WideHistoryFilter, schema: WideSchema) -> str:
        fields = (
            filter_.wind_farm_id, filter_.device_id,
            filter_.device_config_id, filter_.data_level.value,
            filter_.start_time_us, filter_.end_time_us,
            filter_.point_ids, filter_.qualities, filter_.limit, schema.fingerprint,
        )
        return hashlib.sha256(
            json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def encode(
        self,
        filter_: WideHistoryFilter,
        schema: WideSchema,
        *,
        last_us: int,
        horizon_us: int,
    ) -> str:
        raw = json.dumps(
            {
                "v": 1,
                "id": self._identity(filter_, schema),
                "last": last_us,
                "horizon": horizon_us,
                "expires": int(time.time()) + self._ttl,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(hmac.digest(self._key, raw, "sha256") + raw).decode()

    def decode(
        self,
        token: str,
        filter_: WideHistoryFilter,
        schema: WideSchema,
    ) -> tuple[int, int]:
        if not token or len(token) > 4096:
            raise ValueError("wide pagination token absent or exceeds 4 KiB")
        try:
            packed = base64.b64decode(token, altchars=b"-_", validate=True)
            signature, raw = packed[:32], packed[32:]
            if len(signature) != 32 or not hmac.compare_digest(
                signature, hmac.digest(self._key, raw, "sha256")
            ):
                raise ValueError("wide pagination signature is invalid")
            payload = json.loads(raw)
            if (
                not isinstance(payload, dict) or payload.get("v") != 1
                or payload.get("id") != self._identity(filter_, schema)
                or type(payload.get("last")) is not int
                or type(payload.get("horizon")) is not int
                or type(payload.get("expires")) is not int
                or payload["expires"] < int(time.time())
                or not filter_.start_time_us <= payload["last"] < filter_.end_time_us
                or not filter_.start_time_us < payload["horizon"] <= filter_.end_time_us
            ):
                raise ValueError("wide pagination token expired or belongs to another query")
            return payload["last"], payload["horizon"]
        except (ValueError, TypeError, KeyError, UnicodeError, IndexError) as exc:
            raise ValueError("wide pagination token is malformed or unauthorized") from exc


class WideHistoryQueryService:
    """Read exactly one frozen device/schema with bounded concurrent queries."""

    def __init__(
        self,
        *,
        backend: WideQueryBackend,
        codec: WideCursorCodec,
        max_page_size: int = 1000,
        max_range_days: int = 31,
        max_concurrent: int = 4,
        timeout_ms: int = 30000,
        read_available: Callable[[], bool] | None = None,
    ) -> None:
        if (
            not 1 <= max_page_size <= 1000
            or not 1 <= max_range_days <= 365
            or not 1 <= max_concurrent <= 32
            or not 100 <= timeout_ms <= 120000
        ):
            raise ValueError("wide query bounds are invalid")
        self._backend = backend
        self._codec = codec
        self._max_page_size = max_page_size
        self._max_range_days = max_range_days
        self._timeout = timeout_ms / 1000
        self._limiter = asyncio.Semaphore(max_concurrent)
        self._read_available = read_available or (lambda: True)

    async def query(
        self,
        *,
        schema: WideSchema,
        filter_: WideHistoryFilter,
        device_type: str,
        page_token: str = "",
    ) -> WideHistoryPage:
        if not self._read_available():
            raise RuntimeError("wide history is unavailable during Kafka replay")
        filter_.validate(schema, maximum=self._max_page_size)
        if filter_.after_time_us is not None:
            raise ValueError("wide query cursor must be a signed page token")
        if not 0 <= filter_.start_time_us < filter_.end_time_us:
            raise ValueError("wide query requires positive UTC microsecond timestamps")
        if filter_.end_time_us - filter_.start_time_us > (
            self._max_range_days * 86_400_000_000
        ):
            raise ValueError("wide query range exceeds configured maximum")
        last: int | None = None
        horizon = min(filter_.end_time_us, time.time_ns() // 1000)
        if page_token:
            last, horizon = self._codec.decode(page_token, filter_, schema)
        if horizon <= filter_.start_time_us:
            return WideHistoryPage((), "", True, schema.fingerprint)
        request = WideHistoryFilter(
            wind_farm_id=filter_.wind_farm_id,
            device_id=filter_.device_id,
            device_config_id=filter_.device_config_id,
            data_level=filter_.data_level,
            start_time_us=filter_.start_time_us,
            end_time_us=horizon,
            point_ids=filter_.point_ids,
            limit=filter_.limit + 1,
            after_time_us=last,
        )
        async with self._limiter:
            async with asyncio.timeout(self._timeout):
                await self._backend.verify_existing_wide_table(
                    schema,
                    wind_farm_id=filter_.wind_farm_id,
                    device_id=filter_.device_id,
                    device_type=device_type,
                )
                rows = tuple(
                    await self._backend.fetch_wide_history(schema, request)
                )
        if not self._read_available():
            raise RuntimeError("Kafka replay changed while reading device-wide history")
        if len(rows) > request.limit:
            raise RuntimeError("wide backend returned rows beyond requested bound")
        if any(
            row.sample_time_us < filter_.start_time_us
            or row.sample_time_us >= horizon
            or (last is not None and row.sample_time_us <= last)
            for row in rows
        ):
            raise RuntimeError("wide backend returned timestamp outside the cursor range")
        if any(a.sample_time_us >= b.sample_time_us for a, b in zip(rows, rows[1:])):
            raise RuntimeError("wide backend violated strictly ordered timestamp keyset")
        more = len(rows) > filter_.limit
        included = rows[:filter_.limit]
        token = (
            self._codec.encode(
                filter_, schema, last_us=included[-1].sample_time_us,
                horizon_us=horizon,
            )
            if more and included else ""
        )
        return WideHistoryPage(included, token, not more, schema.fingerprint)
