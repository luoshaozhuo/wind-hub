"""Validated historical storage queries with signed keyset pagination.

All query execution is bounded by requested record count, time span, and
registered Storage-owned tables. Never query arbitrary database objects.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Sequence

from storage_service.domain.sample import DataLevel


class QueryRejected(ValueError):
    """Request exceeds the documented Storage query and pagination contract."""


@dataclass(frozen=True, slots=True)
class HistoryFilter:
    wind_farm_id: str
    data_level: DataLevel
    start_us: int
    end_us: int
    device_type: str = ""
    device_id: str = ""
    point_table_id: str = ""
    point_id: str = ""
    business_point_id: str = ""
    qualities: tuple[str, ...] = ()

    def validate(self, *, max_range_days: int) -> None:
        if not self.wind_farm_id:
            raise QueryRejected("wind_farm_id is required")
        if not 0 <= self.start_us < self.end_us:
            raise QueryRejected("start must precede end in UTC microseconds")
        if self.end_us - self.start_us > max_range_days * 86_400_000_000:
            raise QueryRejected("query time range exceeds configured bound")
        if len(self.qualities) > 3 or not set(self.qualities) <= {
            "good", "bad", "uncertain",
        }:
            raise QueryRejected("unknown or excessive sample qualities")
        for value in (
            self.wind_farm_id, self.device_type, self.device_id,
            self.point_table_id, self.point_id, self.business_point_id,
        ):
            if len(value.encode("utf-8")) > 256:
                raise QueryRejected("query identity exceeds 256 UTF-8 bytes")
        if self.data_level != DataLevel.BUSINESS and self.business_point_id:
            raise QueryRejected("business_point_id is valid only for BUSINESS queries")

    def fingerprint(self) -> str:
        fields = [
            self.wind_farm_id, self.data_level.value, self.start_us, self.end_us,
            self.device_type, self.device_id, self.point_table_id,
            self.point_id, self.business_point_id, sorted(self.qualities),
        ]
        return hashlib.sha256(
            json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryRow:
    ts_us: int
    logical_key: str
    content_sha256: str
    identity: tuple[str, ...]
    sample_id: str
    point_id: str
    business_point_id: str
    value_kind: int
    value: int | float | bool | str | bytes | None
    quality: str
    publish_time_us: int
    receive_time_us: int | None

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (self.ts_us, self.logical_key, self.content_sha256)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    rows: tuple[HistoryRow, ...]
    next_page_token: str
    complete: bool


class HistoryBackend(Protocol):
    async def fetch_history(
        self, filter_: HistoryFilter, *,
        after: tuple[int, str, str] | None, limit: int,
    ) -> Sequence[HistoryRow]:
        """Retrieve globally earliest rows from verified and managed tables only."""


class CursorCodec:
    """Opaque HMAC-SHA256 query cursors with bounded lifetime and filter binding."""

    def __init__(self, secret: bytes, *, ttl_seconds: int = 900) -> None:
        if len(secret) < 32 or not 1 <= ttl_seconds <= 3600:
            raise ValueError("cursor requires a strong secret and bounded TTL")
        self._secret = secret
        self._ttl = ttl_seconds

    def encode(
        self, filter_: HistoryFilter, last: tuple[int, str, str], *,
        query_deadline_us: int,
    ) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "filter": filter_.fingerprint(),
                "last": list(last),
                "deadline_us": query_deadline_us,
                "exp": int(time.time()) + self._ttl,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode()
        mac = hmac.digest(self._secret, payload, "sha256")
        return base64.urlsafe_b64encode(mac + payload).decode("ascii")

    def decode(
        self, token: str, filter_: HistoryFilter,
    ) -> tuple[tuple[int, str, str], int]:
        if not token or len(token) > 4096:
            raise QueryRejected("invalid pagination token")
        try:
            packed = base64.b64decode(token.encode(), altchars=b"-_", validate=True)
            signature, raw = packed[:32], packed[32:]
            if len(signature) != 32 or not hmac.compare_digest(
                signature, hmac.digest(self._secret, raw, "sha256")
            ):
                raise QueryRejected("invalid pagination signature")
            decoded = json.loads(raw)
            last = decoded["last"]
            if (
                decoded["v"] != 1
                or decoded["filter"] != filter_.fingerprint()
                or decoded["exp"] < int(time.time())
                or not isinstance(last, list)
                or len(last) != 3
                or type(last[0]) is not int
                or any(not re.fullmatch("[0-9a-f]{64}", key) for key in last[1:])
            ):
                raise QueryRejected("pagination token is expired or belongs to another query")
            deadline = decoded["deadline_us"]
            if type(deadline) is not int or deadline <= 0:
                raise QueryRejected("invalid pagination horizon")
            return (last[0], last[1], last[2]), deadline
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, IndexError) as exc:
            raise QueryRejected("invalid pagination token") from exc


class HistoryQueryService:
    """A bounded keyset page; no OFFSET-based pagination or unstable ordering."""

    def __init__(
        self, backend: HistoryBackend, codec: CursorCodec, *,
        max_page_size: int = 1000,
        max_range_days: int = 31,
        max_concurrent: int = 4,
        timeout_ms: int = 30000,
    ) -> None:
        if not 1 <= max_page_size <= 1000:
            raise ValueError("max page size must be 1..1000")
        self._backend = backend
        self._codec = codec
        self._max_page_size = max_page_size
        self._max_range_days = max_range_days
        if not 1 <= max_concurrent <= 32 or not 100 <= timeout_ms <= 120000:
            raise ValueError("query concurrency or timeout out of bounds")
        self._limit = asyncio.Semaphore(max_concurrent)
        self._timeout = timeout_ms / 1000
        self._active_queries = 0

    @property
    def active_queries(self) -> int:
        return self._active_queries

    async def query(
        self, filter_: HistoryFilter, *, page_size: int = 200,
        page_token: str = "",
    ) -> HistoryPage:
        filter_.validate(max_range_days=self._max_range_days)
        if not 1 <= page_size <= self._max_page_size:
            raise QueryRejected("page_size outside configured range")
        horizon = int(datetime.now(UTC).timestamp() * 1_000_000)
        after: tuple[int, str, str] | None = None
        if page_token:
            after, horizon = self._codec.decode(page_token, filter_)
        # The horizon excludes future new samples. Backfilled historical rows
        # remain allowed and will appear in a new query, not a stale page.
        bounded = HistoryFilter(
            wind_farm_id=filter_.wind_farm_id, data_level=filter_.data_level,
            start_us=filter_.start_us, end_us=min(filter_.end_us, horizon),
            device_type=filter_.device_type, device_id=filter_.device_id,
            point_table_id=filter_.point_table_id, point_id=filter_.point_id,
            business_point_id=filter_.business_point_id, qualities=filter_.qualities,
        )
        if bounded.end_us <= bounded.start_us:
            return HistoryPage((), "", True)
        async with self._limit:
            self._active_queries += 1
            try:
                try:
                    rows = tuple(await asyncio.wait_for(
                        self._backend.fetch_history(
                            bounded, after=after, limit=page_size + 1,
                        ),
                        timeout=self._timeout,
                    ))
                except TimeoutError as exc:
                    raise RuntimeError(
                        "Storage historical query exceeded deadline"
                    ) from exc
            finally:
                self._active_queries -= 1
        if len(rows) > page_size + 1:
            raise RuntimeError("backend violated bounded page contract")
        selected = rows[:page_size]
        if selected and any(
            selected[index].sort_key >= selected[index + 1].sort_key
            for index in range(len(selected) - 1)
        ):
            raise RuntimeError("backend rows are not strictly sorted")
        has_more = len(rows) > page_size
        next_token = (
            self._codec.encode(filter_, selected[-1].sort_key, query_deadline_us=horizon)
            if has_more else ""
        )
        return HistoryPage(selected, next_token, not has_more)
