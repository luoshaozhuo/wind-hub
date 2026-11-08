"""Strict version-2 batched NATS telemetry contract.

The batch is the transport and JetStream acknowledgment unit; individual
samples retain independent identities, timestamps, quality, and TTL.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DeliveryMode = Literal["realtime", "reliable"]
DEFAULT_MAX_SAMPLES_PER_BATCH = 500
DEFAULT_MAX_BATCH_BYTES = 1_048_576


def validate_subject_token(value: str) -> str:
    """Require a nonempty single NATS subject token."""
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char in ".*>" for char in value):
        raise ValueError("invalid NATS subject token")
    return value


def _reject_nonfinite_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON object keys instead of silently overwriting."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_wire_timestamp(value: object, field: str) -> None:
    """Wire time must be a timezone-bearing ISO string, not an epoch."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class SampleItem(BaseModel):
    """One immutable sample within a batch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    point_id: str
    sample_id: str
    value: float | int | bool | str | None
    quality: Literal["good", "bad", "uncertain"]
    sample_time: datetime
    receive_time: datetime | None = None
    max_staleness_ms: int | None = Field(default=None, gt=0, le=86_400_000)

    @field_validator("point_id")
    @classmethod
    def _point_token(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid point_id")
        return value

    @field_validator("sample_id")
    @classmethod
    def _sample_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sample_id must not be empty")
        return value

    @field_validator("sample_time", "receive_time")
    @classmethod
    def _timestamps(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _finite(self) -> SampleItem:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("nonfinite sample value")
        if isinstance(self.value, int) and not isinstance(self.value, bool):
            if not -(2**63) <= self.value <= 2**64 - 1:
                raise ValueError("sample integer exceeds protobuf scalar range")
        return self


class SampleBatch(BaseModel):
    """Versioned, bounded batch with a stable source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    batch_id: str
    mode: DeliveryMode
    wind_farm_id: str
    collector_id: str
    task_id: str
    device_id: str
    publish_time: datetime
    samples: tuple[SampleItem, ...]

    @field_validator("wind_farm_id", "collector_id", "task_id", "device_id")
    @classmethod
    def _identity_token(cls, value: str) -> str:
        return validate_subject_token(value)

    @field_validator("batch_id")
    @classmethod
    def _batch_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("batch_id must not be empty")
        return value

    @field_validator("publish_time")
    @classmethod
    def _publish_timestamp(cls, value: datetime) -> datetime:
        normalized = _utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def _validate_batch(self) -> SampleBatch:
        if not self.samples:
            raise ValueError("batch must contain samples")
        point_ids = [sample.point_id for sample in self.samples]
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("duplicate point_id in batch")
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("duplicate sample_id in batch")
        for sample in self.samples:
            if self.mode == "realtime" and sample.max_staleness_ms is None:
                raise ValueError("realtime sample requires max_staleness_ms")
            if self.mode == "reliable" and sample.max_staleness_ms is not None:
                raise ValueError("reliable sample forbids max_staleness_ms")
        return self

    @property
    def identity(self) -> tuple[str, str, str, str, str, str]:
        """Scope a batch ID to its immutable producer identity."""
        return (
            self.wind_farm_id,
            self.collector_id,
            self.task_id,
            self.device_id,
            self.mode,
            self.batch_id,
        )

    def subject(self) -> str:
        """Use device-scoped batch subjects, never legacy point subjects."""
        prefix = "wh.rt.batch" if self.mode == "realtime" else "wh.hist.batch"
        return f"{prefix}.{self.wind_farm_id}.{self.collector_id}.{self.device_id}"

    def encode(
        self,
        *,
        max_samples: int = DEFAULT_MAX_SAMPLES_PER_BATCH,
        max_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    ) -> bytes:
        """Encode a bounded canonical JSON payload."""
        if (
            type(max_samples) is not int
            or type(max_bytes) is not int
            or max_samples <= 0
            or max_bytes <= 0
        ):
            raise ValueError("batch limits must be positive integers")
        if len(self.samples) > max_samples:
            raise ValueError("batch exceeds max_samples")
        document = self.model_dump(mode="json")
        for item in document["samples"]:
            if item["receive_time"] is None:
                del item["receive_time"]
            if item["max_staleness_ms"] is None:
                del item["max_staleness_ms"]
        # value=null means an explicitly absent reading, never omit it.
        payload = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > max_bytes:
            raise ValueError("batch exceeds max_bytes")
        return payload

    @classmethod
    def decode(
        cls,
        payload: bytes,
        mode: DeliveryMode,
        *,
        subject: str | None = None,
        max_samples: int = DEFAULT_MAX_SAMPLES_PER_BATCH,
        max_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    ) -> SampleBatch:
        """Reject oversized, malformed, mismatched or ambiguous payloads."""
        if (
            type(max_samples) is not int
            or type(max_bytes) is not int
            or max_samples <= 0
            or max_bytes <= 0
            or len(payload) > max_bytes
        ):
            raise ValueError("invalid batch limits or payload length")
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_json_keys,
                parse_constant=_reject_nonfinite_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid UTF-8 JSON batch") from exc
        if not isinstance(raw, dict):
            raise ValueError("batch envelope must be an object")
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 2:
            raise ValueError("explicit schema_version=2 required")
        _require_wire_timestamp(raw.get("publish_time"), "publish_time")
        items = raw.get("samples")
        if not isinstance(items, list) or not 0 < len(items) <= max_samples:
            raise ValueError("invalid batch sample count")
        if any(not isinstance(item, dict) or "value" not in item for item in items):
            raise ValueError("every sample must explicitly contain value")
        for item in items:
            _require_wire_timestamp(item.get("sample_time"), "sample_time")
            if "receive_time" in item:
                _require_wire_timestamp(item["receive_time"], "receive_time")
        if mode == "reliable" and any("max_staleness_ms" in item for item in items):
            raise ValueError("reliable samples must omit max_staleness_ms")
        batch = cls.model_validate_json(payload)
        if batch.mode != mode:
            raise ValueError("batch mode differs from NATS channel")
        if subject is not None and batch.subject() != subject:
            raise ValueError("batch source differs from NATS subject")
        return batch
