"""Strict Kafka JSON v3 device-wide record, ordered by the resolved physical schema."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from storage_service.domain.quality_bitmap import pack_quality_bitmap


class WideDataLevel(StrEnum):
    RAW = "raw"
    PROTOCOL = "protocol"
    BUSINESS = "business"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _sample_time_us(text: object) -> int:
    if not isinstance(text, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", text
    ):
        raise ValueError("sample_time requires UTC RFC3339 with microsecond precision")
    try:
        instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid sample_time") from exc
    delta = instant - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class WideRecord:
    """One complete logical device sample at a single data level and timestamp."""

    wind_farm_id: str
    device_config_id: str
    device_id: str
    data_level: WideDataLevel
    sample_time_us: int
    point_ids: tuple[str, ...]
    values: tuple[int | float | bool | str | None, ...]
    quality_bitmap: bytes

    @classmethod
    def from_kafka_json(cls, raw: bytes, *, physical_points: Sequence[str]) -> WideRecord:
        """Reject missing/extra points instead of time-aligning or patching partial rows."""
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Kafka wide record exceeds 16 MiB")
        try:
            data = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid Kafka JSON v3") from exc
        if not isinstance(data, dict):
            raise ValueError("Kafka message must be an object")
        required = {
            "schema_version", "wind_farm_id", "device_config_id",
            "device_id", "data_level", "sample_time", "points",
        }
        if set(data) != required or type(data["schema_version"]) is not int or data["schema_version"] != 3:
            raise ValueError("Kafka JSON v3 envelope mismatch")
        identities = (data["wind_farm_id"], data["device_config_id"], data["device_id"])
        if any(
            not isinstance(item, str) or not item or len(item.encode("utf-8")) > 256
            for item in identities
        ):
            raise ValueError("device-wide record identities must be nonempty UTF-8")
        try:
            level = WideDataLevel(data["data_level"])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid data_level") from exc
        ordered = tuple(physical_points)
        if not ordered or len(set(ordered)) != len(ordered):
            raise ValueError("physical schema must contain unique point IDs")
        if not all(isinstance(point, str) and point for point in ordered):
            raise ValueError("invalid physical point ID")
        points = data["points"]
        if not isinstance(points, dict) or set(points) != set(ordered):
            raise ValueError("wide record must contain the complete physical point set")
        values: list[int | float | bool | str | None] = []
        qualities: list[int] = []
        for point in ordered:
            entry = points[point]
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError(f"{point}: expected [value, quality]")
            value, quality = entry
            if type(quality) is not int or not 0 <= quality <= 3:
                raise ValueError(f"{point}: invalid two-bit quality")
            if value is not None and type(value) not in (int, float, bool, str):
                raise ValueError(f"{point}: unsupported JSON scalar")
            if type(value) is float and not math.isfinite(value):
                raise ValueError(f"{point}: nonfinite value")
            values.append(value)
            qualities.append(quality)
        return cls(
            wind_farm_id=identities[0],
            device_config_id=identities[1],
            device_id=identities[2],
            data_level=level,
            sample_time_us=_sample_time_us(data["sample_time"]),
            point_ids=ordered,
            values=tuple(values),
            quality_bitmap=pack_quality_bitmap(qualities),
        )
