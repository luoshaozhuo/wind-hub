"""Strict versioned batch transport contract for both Sink Service channels."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.sample_message import DeliveryMode, validate_subject_token

BATCH_SCHEMA_VERSION = 2
MAX_SAMPLES_PER_BATCH = 1024
MAX_BATCH_BYTES = 1_048_576
REALTIME_BATCH_PREFIX = "wh.batch.rt"
RELIABLE_BATCH_PREFIX = "wh.batch.hist"

SampleValue = float | int | bool | str | None


class BatchSample(BaseModel):
    """One immutable measurement in a device-scoped transport batch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    point_id: str
    sample_id: str
    value: SampleValue
    quality: Literal["good", "bad", "uncertain"]
    sample_time: datetime
    receive_time: datetime | None = None
    max_staleness_ms: int | None = Field(default=None, gt=0, le=86_400_000)

    @field_validator("point_id")
    @classmethod
    def check_point_id(cls, value: str) -> str:
        return validate_subject_token(value)

    @field_validator("sample_id")
    @classmethod
    def check_sample_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sample_id must not be empty")
        return value

    @field_validator("sample_time", "receive_time")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sample timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def check_value(self) -> BatchSample:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("value must be finite")
        return self


class SampleBatch(BaseModel):
    """Exactly one device, collector and delivery mode per source message."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = BATCH_SCHEMA_VERSION
    batch_id: str
    mode: DeliveryMode
    wind_farm_id: str
    collector_id: str
    task_id: str
    device_id: str
    publish_time: datetime
    samples: tuple[BatchSample, ...] = Field(
        min_length=1, max_length=MAX_SAMPLES_PER_BATCH
    )

    @field_validator("wind_farm_id", "collector_id", "task_id", "device_id")
    @classmethod
    def check_source_token(cls, value: str) -> str:
        return validate_subject_token(value)

    @field_validator("batch_id")
    @classmethod
    def check_batch_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("batch_id must not be empty")
        return value

    @field_validator("publish_time")
    @classmethod
    def normalize_publish_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publish_time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def check_samples(self) -> SampleBatch:
        point_ids: set[str] = set()
        sample_ids: set[str] = set()
        for sample in self.samples:
            if sample.point_id in point_ids or sample.sample_id in sample_ids:
                raise ValueError("duplicate point_id or sample_id in batch")
            point_ids.add(sample.point_id)
            sample_ids.add(sample.sample_id)
            if self.mode == "realtime" and sample.max_staleness_ms is None:
                raise ValueError("realtime sample requires max_staleness_ms")
            if self.mode == "reliable" and sample.max_staleness_ms is not None:
                raise ValueError("reliable sample must omit max_staleness_ms")
        return self

    def subject(self) -> str:
        """Keep v2 batch subjects distinct from the v1 single-point subjects."""
        prefix = (
            REALTIME_BATCH_PREFIX if self.mode == "realtime"
            else RELIABLE_BATCH_PREFIX
        )
        return (
            f"{prefix}.{self.wind_farm_id}.{self.collector_id}.{self.device_id}"
        )

    def encode(self) -> bytes:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_BATCH_BYTES:
            raise ValueError("batch payload exceeds maximum byte length")
        return payload

    @classmethod
    def decode(
        cls, payload: bytes, *, mode: DeliveryMode, subject: str | None = None
    ) -> SampleBatch:
        """Reject oversized, malformed, wrong-channel or misrouted payloads."""
        if len(payload) > MAX_BATCH_BYTES:
            raise ValueError("batch payload exceeds maximum byte length")
        batch = cls.model_validate_json(payload)
        if batch.mode != mode:
            raise ValueError("batch delivery mode mismatch")
        if subject is not None and batch.subject() != subject:
            raise ValueError("batch subject/source mismatch")
        return batch
