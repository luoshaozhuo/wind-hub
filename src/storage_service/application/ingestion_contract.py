"""Shared Kafka partition records, acknowledgements, and ingest failure semantics."""

from dataclasses import dataclass


class ConfigHistoryUnavailableError(RuntimeError):
    """Historical configuration is absent, corrupt, or cannot be validated."""


class IngestionBlockedError(RuntimeError):
    """A deterministic bad record requires operator intervention."""


class WriteUnconfirmedError(RuntimeError):
    """A write has uncertain durability and cannot confirm its Kafka offset."""


@dataclass(frozen=True, slots=True)
class KafkaRecord:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes


@dataclass(frozen=True, slots=True)
class IngestResult:
    next_commit_offset: int
    accepted_records: int
    replayed_records: int
