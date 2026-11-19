"""Application policy for immutable Collector sample and batch identities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from contracts.sample_batch import SampleBatch, SampleItem, validate_subject_token
from core.application import ProtocolSample


class BatchBuilder:
    """Validate one acquisition event and split it into bounded immutable batches."""

    def __init__(
        self,
        *,
        site_id: str,
        collector_id: str,
        delivery: Literal["realtime", "reliable"],
        max_staleness_ms: int,
        max_samples_per_batch: int,
        max_batch_bytes: int,
    ) -> None:
        if delivery not in {"realtime", "reliable"}:
            raise ValueError("unsupported delivery mode")
        if max_samples_per_batch <= 0 or max_batch_bytes <= 0:
            raise ValueError("batch limits must be positive")
        if delivery == "realtime" and max_staleness_ms <= 0:
            raise ValueError("realtime max_staleness_ms must be positive")
        self._site_id = validate_subject_token(site_id)
        self.collector_id = validate_subject_token(collector_id)
        self._delivery = delivery
        self._staleness = max_staleness_ms
        self._max_samples = max_samples_per_batch
        self._max_bytes = max_batch_bytes

    def update_site(self, site_id: str) -> None:
        self._site_id = validate_subject_token(site_id)

    def _item(self, sample: ProtocolSample) -> SampleItem:
        return SampleItem(
            point_id=sample.point_id,
            sample_id=uuid4().hex,
            value=sample.value,
            quality=sample.quality.value,
            sample_time=sample.timestamp,
            receive_time=sample.receive_time,
            max_staleness_ms=self._staleness if self._delivery == "realtime" else None,
        )

    def _batch(
        self,
        device_id: str,
        device_type: str,
        point_table_id: str,
        samples: tuple[SampleItem, ...],
    ) -> SampleBatch:
        return SampleBatch(
            batch_id=uuid4().hex,
            mode=self._delivery,
            wind_farm_id=self._site_id,
            collector_id=self.collector_id,
            task_id=device_id,
            device_type=device_type,
            device_id=device_id,
            point_table_id=point_table_id,
            publish_time=datetime.now(UTC),
            samples=samples,
        )

    def pack(
        self,
        device_id: str,
        samples: tuple[ProtocolSample, ...],
        *,
        device_type: str,
        point_table_id: str,
    ) -> list[SampleBatch]:
        if not samples:
            return []
        point_ids = [sample.point_id for sample in samples]
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("duplicate point_id in acquisition batch")
        device_type = validate_subject_token(device_type)
        if not point_table_id.startswith("point_table_") or "_v" not in point_table_id:
            raise ValueError("immutable point_table_id required")
        items = tuple(self._item(sample) for sample in samples)
        batches: list[SampleBatch] = []
        start = 0
        while start < len(items):
            count = min(self._max_samples, len(items) - start)
            candidate = self._batch(device_id, device_type, point_table_id, items[start : start + count])
            while count:
                try:
                    candidate.encode(max_samples=self._max_samples, max_bytes=self._max_bytes)
                    break
                except ValueError:
                    if count == 1:
                        raise
                    count //= 2
                    candidate = self._batch(device_id, device_type, point_table_id, items[start : start + count])
            batches.append(candidate)
            start += count
        return batches
