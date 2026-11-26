"""Version-pinned wide aggregation with bounded snapshots and signed pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, replace
from math import isfinite

from storage_service.application.wide_query import WideHistoryFilter, WideHistoryQueryService
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_schema import WideSchema

_FLOAT = {"FLOAT", "DOUBLE"}

_INTEGER_BOUNDS = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "TINYINT UNSIGNED": (0, 2**8 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "SMALLINT UNSIGNED": (0, 2**16 - 1),
    "INT": (-(2**31), 2**31 - 1),
    "INT UNSIGNED": (0, 2**32 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "BIGINT UNSIGNED": (0, 2**64 - 1),
}

_INTEGER = frozenset(_INTEGER_BOUNDS)


def _numeric_value(value: object, sql_type: str) -> int | float:
    """Reject corrupted TDengine values before producing typed aggregates."""
    if sql_type in _INTEGER_BOUNDS:
        minimum, maximum = _INTEGER_BOUNDS[sql_type]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("wide integer aggregate exceeds declared SQL type")
        return value
    if sql_type in _FLOAT and (type(value) is int or type(value) is float):
        try:
            result = float(value)
        except OverflowError as exc:
            raise ValueError("wide floating-point aggregate overflow") from exc
        if isfinite(result):
            return result
    raise ValueError("wide numeric aggregate encountered invalid physical value")


@dataclass(frozen=True, slots=True)
class WideAggregateRow:
    point_id: str
    start_us: int
    end_us: int
    good_count: int
    missing_count: int
    result: int | float | None
    result_kind: int  # 0=missing, 1=signed, 2=unsigned, 3=double
    valid_count: int = 0


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
        if (
            len(secret) < 32
            or type(max_page_size) is not int
            or not 1 <= max_page_size <= 1000
            or type(max_scan_rows) is not int
            or max_scan_rows < 1
            or type(max_groups) is not int
            or max_groups < 1
        ):
            raise ValueError("wide aggregate requires a strong signing key and bounded pages")
        self._history = history
        self._key = secret
        self._page_size = max_page_size
        self._max_scan = max_scan_rows
        self._max_groups = max_groups
        self._snapshots: dict[str, tuple[int, str, tuple[WideAggregateRow, ...]]] = {}

    @staticmethod
    def _fingerprint(
        schema: WideSchema,
        filter_: WideHistoryFilter,
        operation: str,
        window_ms: int,
        page_size: int,
    ) -> str:
        payload = json.dumps(
            [
                schema.fingerprint,
                filter_.wind_farm_id,
                filter_.device_id,
                filter_.device_config_id,
                filter_.data_level.value,
                filter_.start_time_us,
                filter_.end_time_us,
                filter_.point_ids,
                filter_.qualities,
                operation,
                window_ms,
                page_size,
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _encode(self, name: str, digest: str, next_index: int, expires: int) -> str:
        payload = json.dumps(
            [name, digest, next_index, expires],
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(
            hmac.digest(self._key, payload, "sha256") + payload
        ).decode()

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
                not isinstance(name, str)
                or not isinstance(binding, str)
                or binding != digest
                or type(index) is not int
                or index < 0
                or type(expires) is not int
                or expires < time.time()
            ):
                raise ValueError("wide aggregate cursor is expired or rebound")
            return name, index
        except (ValueError, TypeError, IndexError, UnicodeError) as exc:
            raise ValueError("wide aggregate cursor is malformed") from exc

    async def _compute(
        self,
        *,
        schema: WideSchema,
        filter_: WideHistoryFilter,
        device_type: str,
        operation: str,
        window_ms: int,
    ) -> tuple[WideAggregateRow, ...]:
        by_point = {col.point_id: col for col in schema.columns}
        if operation not in {"count", "min", "max", "avg"}:
            raise ValueError("wide aggregate operation must be count/min/max/avg")
        if type(window_ms) is not int or not 1 <= window_ms <= 31 * 86_400_000:
            raise ValueError("wide aggregate window is invalid")
        if operation != "count" and any(
            by_point[point_id].sql_type not in _INTEGER | _FLOAT for point_id in filter_.point_ids
        ):
            raise ValueError("wide min/max/avg requires numeric columns")
        # Keep one streaming accumulator per window/point, not every source value.
        aggregates: dict[tuple[int, str], int | float] = {}
        counts: dict[tuple[int, str], tuple[int, int, int]] = {}
        # Scan unfiltered rows: quality selection must not change diagnostic counts.
        scan_filter = replace(filter_, qualities=())
        accepted_qualities = (
            set(filter_.qualities) if filter_.qualities else {int(PointQuality.GOOD)}
        )
        cursor = ""
        visited: set[str] = set()
        scanned = 0
        while True:
            page = await self._history.query(
                schema=schema,
                filter_=scan_filter,
                device_type=device_type,
                page_token=cursor,
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
                    good, missing, valid = counts.get(key, (0, 0, 0))
                    if value is None:
                        missing += 1
                    else:
                        if int(quality) == int(PointQuality.GOOD):
                            good += 1
                        if int(quality) in accepted_qualities:
                            if operation != "count":
                                physical = by_point[point_id].sql_type
                                numeric = _numeric_value(value, physical)
                                previous = aggregates.get(key)
                                if operation == "min":
                                    aggregates[key] = (
                                        numeric if previous is None else min(previous, numeric)
                                    )
                                elif operation == "max":
                                    aggregates[key] = (
                                        numeric if previous is None else max(previous, numeric)
                                    )
                                elif physical in _INTEGER:
                                    value_int = int(numeric)
                                    previous_total = int(previous) if previous is not None else 0
                                    total = previous_total + value_int
                                    if abs(value_int) > 2**53 or abs(total) > 2**53:
                                        raise ValueError("AVG would silently lose integer precision")
                                    aggregates[key] = total
                                else:
                                    aggregates[key] = (
                                        float(previous) if previous is not None else 0.0
                                    ) + float(numeric)
                            valid += 1
                    counts[key] = (good, missing, valid)
            if page.complete:
                break
            if not page.next_page_token or page.next_page_token in visited:
                raise RuntimeError("wide aggregate pagination stalled")
            visited.add(page.next_page_token)
            cursor = page.next_page_token
        result: list[WideAggregateRow] = []
        for (start, point_id), (good, missing, valid) in sorted(counts.items()):
            physical = by_point[point_id].sql_type
            actual: int | float | None = None
            kind = 0
            if operation == "count":
                actual, kind = valid, 2
            elif (start, point_id) in aggregates:
                accumulator = aggregates[(start, point_id)]
                if operation == "avg":
                    actual = float(accumulator) / valid
                    if not isfinite(actual):
                        raise ValueError("wide AVG result is not finite")
                else:
                    actual = accumulator
                kind = (
                    3
                    if operation == "avg" or physical in _FLOAT
                    else 2
                    if physical.endswith(" UNSIGNED")
                    else 1
                )
            result.append(
                WideAggregateRow(
                    point_id=point_id,
                    start_us=start,
                    end_us=start + window_ms * 1000,
                    good_count=good,
                    missing_count=missing,
                    valid_count=valid,
                    result=actual,
                    result_kind=kind,
                )
            )
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
        if type(page_size) is not int or not 1 <= page_size <= self._page_size:
            raise ValueError("wide aggregate page size exceeds configured limit")
        replace(filter_, qualities=()).validate(schema, maximum=self._page_size)
        if len(set(filter_.qualities)) != len(filter_.qualities) or any(
            type(quality) is not int or not 0 <= quality <= 3 for quality in filter_.qualities
        ):
            raise ValueError("wide aggregate quality filter contains invalid states")
        if filter_.after_time_us is not None:
            raise ValueError("wide aggregate requires signed pagination only")
        if not self._history.read_available():
            raise RuntimeError("wide aggregate unavailable during Kafka replay")
        identity = self._fingerprint(schema, filter_, operation, window_ms, page_size)
        now = int(time.time())
        self._snapshots = {key: entry for key, entry in self._snapshots.items() if entry[0] >= now}
        if page_token:
            name, index = self._decode(page_token, identity)
            stored = self._snapshots.get(name)
            if stored is None or stored[1] != identity:
                raise ValueError("wide aggregate snapshot unavailable")
            records = stored[2]
        else:
            records = await self._compute(
                schema=schema,
                filter_=filter_,
                device_type=device_type,
                operation=operation,
                window_ms=window_ms,
            )
            name, index = uuid.uuid4().hex, 0
            if len(records) > page_size:
                if len(self._snapshots) >= 16:
                    oldest = min(self._snapshots, key=lambda key: self._snapshots[key][0])
                    del self._snapshots[oldest]
                self._snapshots[name] = (now + 900, identity, records)
        if index > len(records):
            raise ValueError("wide aggregate cursor index out of bounds")
        segment = records[index : index + page_size]
        next_index = index + len(segment)
        complete = next_index >= len(records)
        token = (
            ""
            if complete
            else self._encode(
                name,
                identity,
                next_index,
                self._snapshots[name][0],
            )
        )
        if not self._history.read_available():
            raise RuntimeError("wide aggregate replay changed during pagination")
        return WideAggregatePage(segment, token, complete)
