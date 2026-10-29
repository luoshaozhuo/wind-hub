"""Versioned, single-sample NATS transport contract."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DeliveryMode = Literal["realtime", "reliable"]


def validate_subject_token(value: str) -> str:
    """Require one safe NATS subject component."""
    if not value or any(char in value for char in (".", "*", ">", " ", "\t", "\r", "\n")):
        raise ValueError("invalid NATS subject token")
    return value


class SampleMessage(BaseModel):
    """One measurement with immutable source and sample identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    wind_farm_id: str
    collector_id: str
    task_id: str
    device_id: str
    point_id: str
    sample_id: str
    value: float | int | bool | str | None
    quality: Literal["good", "bad", "uncertain"]
    sample_time: datetime
    publish_time: datetime
    receive_time: datetime | None = None
    max_staleness_ms: int | None = Field(default=None, gt=0, le=86_400_000)

    @field_validator("wind_farm_id", "collector_id", "task_id", "device_id", "point_id")
    @classmethod
    def validate_token(cls, value: str) -> str:
        """Reject NATS special tokens and ambiguous identity components."""
        return validate_subject_token(value)

    @field_validator("sample_id")
    @classmethod
    def validate_sample_id(cls, value: str) -> str:
        """Reject absent sample identity."""
        if not value.strip():
            raise ValueError("sample_id must not be empty")
        return value

    @field_validator("sample_time", "publish_time", "receive_time")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        """Normalize timezone-aware timestamps to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_value(self) -> SampleMessage:
        """Forbid nonstandard JSON floating-point payloads."""
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("value must be finite")
        return self

    def subject(self, mode: DeliveryMode) -> str:
        """Return the protocol-defined subject for this sample."""
        prefix = "wh.rt" if mode == "realtime" else "wh.hist"
        return (
            f"{prefix}.{self.wind_farm_id}.{self.collector_id}." f"{self.device_id}.{self.point_id}"
        )

    def encode(self) -> bytes:
        """Encode strict UTF-8 JSON."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def decode(cls, payload: bytes, mode: DeliveryMode) -> SampleMessage:
        """Parse a message and enforce delivery-specific expiry metadata."""
        message = cls.model_validate_json(payload)
        if mode == "realtime" and message.max_staleness_ms is None:
            raise ValueError("realtime message requires max_staleness_ms")
        if mode == "reliable" and message.max_staleness_ms is not None:
            raise ValueError("reliable message must omit max_staleness_ms")
        return message
