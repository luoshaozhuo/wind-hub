"""Version-pinned wide aggregation with bounded snapshots and signed pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from math import isfinite

from storage_service.application.wide_query import WideHistoryFilter, WideHistoryQueryService
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_schema import WideSchema

_INTEGER = {
    "TINYINT", "SMALLINT", "INT", "BIGINT",
    "TINYINT UNSIGNED", "SMALLINT UNSIGNED", "INT UNSIGNED", "BIGINT UNSIGNED",
}
_FLOAT = {"FLOAT", "DOUBLE"}


@dataclass(frozen=True, slots=True)
class WideAggregateRow:
    point_id: str
    start_us: int
    end_us: int
    good_count: int
    missing_count: int
    result: int | float | None
    result_kind: int  # 0=missing, 1=signed, 2=unsigned, 3=double


@dataclass(frozen=True, slots=True)
class WideAggregatePage:
    rows: tuple[WideAggregateRow, ...]
    next_page_token: str
    complete: bool


class WideAggregationService:
    """One fixed physical schema and deterministic window aggregation per request."""

    def __init__(
        self,
        *,
        history: WideHistoryQueryService,
        secret: bytes,
        max_page_size: int = 1000,
        max_scan_rows: int = 10000,
        max_groups: int = 10000,
    ) -> None:
        if len(secret) < 32 or not 1 <= max_page_size <= 1000:
            raise ValueError("wide aggregate requires a strong signing key and bounded pages")
        self._history = history
        self._key = secret
        self._page_size = max_page_size
        self._max_scan = max_scan_rows
        self._max_groups = max_groups
        self._snapshots: dict[
            str, tuple[int, str, tuple[WideAggregateRow, ...]]
        ] = {}

    @staticmethod
    def _fingerprint(
        schema: WideSchema,
        filter_: WideHistoryFilter,
        operation: str,
        window_ms: int,
    ) -> str:
        payload = json.dumps(
            [
                schema.fingerprint, filter_.wind_farm_id, filter_.device_id,
                filter_.device_config_id, filter_.data_level.value,
                filter_.start_time_us, filter_.end_time_us, filter_.point_ids,
                operation, window_ms,
            ],
            separators=(",", ":"), ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _encode(self, name: str, digest: str, next_index: int, expires: int) -> str:
        payload = json.dumps(
            [name, digest, next_index, expires], separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(hmac.digest(self._key, payload, "sha256") + payload).decode()

    def _decode(self, token: str, digest: str) -> tuple[str, int]:
        if not token or len(token) > 4096:
            raise ValueError("wide aggregate cursor is invalid")
        try:
            data = base64.b64decode(token, altchars=b"-_", validate=True)
            mac, encoded = data[:32], data[32:]
            if len(mac) != 32 or not hmac.compare_digest(
                mac, hmac.digest(self._key, encoded, "sha256")
            ):
                raise ValueError("wide aggregate cursor signature mismatch")
            name, binding, index, expires = json.loads(encoded)
            if (
                not isinstance(name, str) or not isinstance(binding, str)
                or binding != digest or type(index) is not int or index < 0
                or type(expires) is not int or expires < time.time()
            ):
                raise ValueError("wide aggregate cursor is expired or rebound")
            return name, index
        except (ValueError, TypeError, IndexError, UnicodeError) as exc:
            raise ValueError("wide aggregate cursor is malformed") from exc

    async def _compute(
        self, *,
        schema: WideSchema, filter_: WideHistoryFilter,
        device_type: str, operation: str, window_ms: int,
    ) -> tuple[WideAggregateRow, ...]:
        by_point = {col.point_id: col for col in schema.columns}
        if operation not in {"count", "min", "max", "avg"}:
            raise ValueError("wide aggregate operation must be count/min/max/avg")
        if not 1 <= window_ms <= 31 * 86_400_000:
            raise ValueError("wide aggregate window is invalid")
        if operation != "count" and any(
            by_point[point_id].sql_type not in _INTEGER | _FLOAT
            for point_id in filter_.point_ids
        ):
            raise ValueError("wide min/max/avg requires numeric columns")
        # Values are kept per bounded window only; no raw row buffer.
        values: dict[tuple[int, str], list[int | float]] = {}
        counts: dict[tuple[int, str], tuple[int, int]] = {}
        cursor = ""
        visited: set[str] = set()
        scanned = 0
        while True:
            page = await self._history.query(
                schema=schema, filter_=filter_,
                device_type=device_type, page_token=cursor,
            )
            for row in page.rows:
                scanned += 1
                if scanned > self._max_scan:
                    raise RuntimeError("wide aggregate scan budget exceeded; narrow time range")
                for point_id, value, quality in row.points:
                    key = (
                        (row.sample_time_us // (window_ms * 1000)) * window_ms * 1000,
                        point_id,
                    )
                    if key not in counts and len(counts) >= self._max_groups:
                        raise RuntimeError("wide aggregate group budget exceeded")
                    good, missing = counts.get(key, (0, 0))
                    if quality is PointQuality.NOT_COLLECTED or value is None:
                        missing += 1
                    elif quality is PointQuality.GOOD:
                        if operation != "count":
                            if type(value) not in (float, int) or (
                                type(value) is float and not isfinite(value)
                            ):
                                raise ValueError("wide numeric aggregate encountered nonnumeric data")
                            values.setdefault(key, []).append(value)
                        good += 1
                    counts[key] = (good, missing)
            if page.complete:
                break
            if not page.next_page_token or page.next_page_token in visited:
                raise RuntimeError("wide aggregate pagination stalled")
            visited.add(page.next_page_token)
            cursor = page.next_page_token
        result: list[WideAggregateRow] = []
        for (start, point_id), (good, missing) in sorted(counts.items()):
            physical = by_point[point_id].sql_type
            entries = values.get((start, point_id), ())
            actual: int | float | None = None
            kind = 0
            if operation == "count":
                actual, kind = good, 2
            elif entries:
                if operation == "min":
                    actual = min(entries)
                elif operation == "max":
                    actual = max(entries)
                else:
                    if physical in _INTEGER and (
                        any(abs(v) > 2**53 for v in entries)
                        or abs(sum(int(value) for value in entries)) > 2**53
                    ):
                        raise ValueError("AVG would silently lose integer precision")
                    actual = sum(float(value) for value in entries) / len(entries)
                kind = (
                    3 if operation == "avg" or physical in _FLOAT
                    else 2 if physical.endswith(" UNSIGNED")
                    else 1
                )
            result.append(WideAggregateRow(
                point_id=point_id, start_us=start,
                end_us=start + window_ms * 1000,
                good_count=good, missing_count=missing,
                result=actual, result_kind=kind,
            ))
        return tuple(result)

    async def page(
        self,
        *,
        schema: WideSchema,
        filter_: WideHistoryFilter,
        device_type: str,
        operation: str,
        window_ms: int,
        page_size: int,
        page_token: str = "",
    ) -> WideAggregatePage:
        if not 1 <= page_size <= self._page_size:
            raise ValueError("wide aggregate page size exceeds configured limit")
        identity = self._fingerprint(schema, filter_, operation, window_ms)
        now = int(time.time())
        self._snapshots = {
            key: entry for key, entry in self._snapshots.items()
            if entry[0] >= now
        }
        if page_token:
            name, index = self._decode(page_token, identity)
            stored = self._snapshots.get(name)
            if stored is None or stored[1] != identity:
                raise ValueError("wide aggregate snapshot unavailable")
            records = stored[2]
        else:
            records = await self._compute(
                schema=schema, filter_=filter_, device_type=device_type,
                operation=operation, window_ms=window_ms,
            )
            name, index = uuid.uuid4().hex, 0
            if len(records) > page_size:
                if len(self._snapshots) >= 16:
                    oldest = min(self._snapshots, key=lambda key: self._snapshots[key][0])
                    del self._snapshots[oldest]
                self._snapshots[name] = (now + 900, identity, records)
        if index > len(records):
            raise ValueError("wide aggregate cursor index out of bounds")
        segment = records[index:index + page_size]
        next_index = index + len(segment)
        complete = next_index >= len(records)
        token = "" if complete else self._encode(
            name, identity, next_index, self._snapshots[name][0],
        )
        return WideAggregatePage(segment, token, complete)
