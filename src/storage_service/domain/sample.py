"""Strict Kafka JSON v2 decoding and stable storage-sample identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any, TypeAlias

from core.domain.sample_value import Quality


class DataLevel(StrEnum):
    """Target storage levels; legacy engineering is not a protocol alias."""

    RAW = "raw"
    PROTOCOL = "protocol"
    BUSINESS = "business"


class ValueKind(IntEnum):
    MISSING = 1
    INT64 = 2
    UINT64 = 3
    DOUBLE = 4
    BOOL = 5
    TEXT = 6
    BYTES = 7


SampleScalar: TypeAlias = int | float | bool | str | bytes | None


def _canonical(values: list[object]) -> bytes:
    return json.dumps(
        values, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256(values: list[object]) -> str:
    return hashlib.sha256(_canonical(values)).hexdigest()


def _required_text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name}: expected nonempty UTF-8 string <= 256 bytes")
    return value


def _optional_text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name}: expected UTF-8 string <= 256 bytes or null")
    return value


def _timestamp_us(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name}: expected RFC 3339 string")
    if re.search(r"\.\d{7,}(?:Z|[+-]\d\d:\d\d)$", value):
        raise ValueError(f"{name}: sub-microsecond precision is unsupported")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}: invalid timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name}: timezone is required")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("message exceeds 4 MiB")

    def _reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON member: {key}")
            result[key] = value
        return result

    try:
        data: Any = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 JSON message") from exc
    if not isinstance(data, dict):
        raise ValueError("message must be a JSON object")
    return data


def _parse_value(value: object) -> tuple[ValueKind, SampleScalar, str]:
    if value is None:
        return ValueKind.MISSING, None, ""
    if type(value) is bool:
        return ValueKind.BOOL, value, str(value).lower()
    if type(value) is int:
        if -(2**63) <= value <= 2**63 - 1:
            return ValueKind.INT64, value, str(value)
        if 0 <= value <= 2**64 - 1:
            return ValueKind.UINT64, value, str(value)
        raise ValueError("integer outside signed/unsigned 64-bit range")
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite floating-point value")
        normalized = 0.0 if value == 0.0 else value
        return ValueKind.DOUBLE, normalized, struct.pack(">d", normalized).hex()
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("text value exceeds VARCHAR(2048) policy")
        return ValueKind.TEXT, value, value
    raise ValueError("value must be scalar/null; RAW bytes require a future wire contract")


@dataclass(frozen=True, slots=True)
class StorageSample:
    """A validated single-point sample with collision-safe content key."""

    wind_farm_id: str
    device_type: str
    device_id: str
    point_table_id: str
    point_id: str
    business_point_id: str
    sample_id: str
    data_level: DataLevel
    quality: Quality
    unit: str
    value_kind: ValueKind
    value: SampleScalar
    sample_time_us: int
    publish_time_us: int
    receive_time_us: int | None
    logical_key: str
    content_sha256: str
    sample_key: str

    @classmethod
    def from_kafka_json(cls, raw: bytes) -> StorageSample:
        """Reject unknown levels and lossy coercions instead of guessing."""
        data = _decode_json_object(raw)
        if type(data.get("schema_version")) is not int or data["schema_version"] != 2:
            raise ValueError("schema_version: only Kafka JSON v2 is supported")

        try:
            level = DataLevel(data["data_level"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("data_level: expected raw/protocol/business") from exc
        try:
            quality = Quality(data["quality"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("quality: expected good/bad/uncertain") from exc

        wind_farm_id = _required_text(data, "wind_farm_id")
        device_type = _required_text(data, "device_type")
        device_id = _required_text(data, "device_id")
        point_table_id = _required_text(data, "point_table_id")
        point_id = _required_text(data, "point_id")
        sample_id = _required_text(data, "sample_id")
        _required_text(data, "collector_id")
        _required_text(data, "task_id")
        _required_text(data, "batch_id")

        business_point_id = _optional_text(data, "business_point_id")
        unit = _optional_text(data, "unit")
        if level == DataLevel.BUSINESS and not business_point_id:
            raise ValueError("business_point_id: required for business")
        if level != DataLevel.BUSINESS and business_point_id:
            raise ValueError("business_point_id: only valid for business")
        if level == DataLevel.RAW and unit:
            raise ValueError("RAW values must not declare engineering units")
        if "value" not in data:
            raise ValueError("value: missing required member")
        kind, value, stable_value = _parse_value(data["value"])
        sample_time_us = _timestamp_us(data.get("sample_time"), "sample_time")
        publish_time_us = _timestamp_us(data.get("publish_time"), "publish_time")
        receive_time_us = _timestamp_us(
            data.get("receive_time"), "receive_time", optional=True
        )
        assert sample_time_us is not None
        assert publish_time_us is not None
        logical_key = _sha256(
            [
                wind_farm_id, device_type, device_id, point_table_id,
                level.value, point_id, business_point_id, sample_time_us, sample_id,
            ]
        )
        content_sha256 = _sha256(
            [int(kind), stable_value, quality.value, business_point_id, unit]
        )
        sample_key = _sha256([logical_key, content_sha256])
        return cls(
            wind_farm_id=wind_farm_id,
            device_type=device_type,
            device_id=device_id,
            point_table_id=point_table_id,
            point_id=point_id,
            business_point_id=business_point_id,
            sample_id=sample_id,
            data_level=level,
            quality=quality,
            unit=unit,
            value_kind=kind,
            value=value,
            sample_time_us=sample_time_us,
            publish_time_us=publish_time_us,
            receive_time_us=receive_time_us,
            logical_key=logical_key,
            content_sha256=content_sha256,
            sample_key=sample_key,
        )
