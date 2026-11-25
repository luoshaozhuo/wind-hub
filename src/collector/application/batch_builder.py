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

    def _item(self, sample: ProtocolSample, sample_time: datetime) -> SampleItem:
        return SampleItem(
            point_id=sample.point_id,
            sample_id=uuid4().hex,
            value=sample.value,
            quality=sample.quality.value,
            sample_time=sample_time,
            source_time=sample.timestamp,
            receive_time=sample.receive_time,
            raw_payload_hex=sample.raw_payload.hex() if sample.raw_payload is not None else None,
            max_staleness_ms=self._staleness if self._delivery == "realtime" else None,
        )

    def _batch(
        self,
        device_id: str,
        task_id: str,
        device_type: str,
        point_table_id: str,
        device_config_id: str,
        sample_time: datetime,
        samples: tuple[SampleItem, ...],
    ) -> SampleBatch:
        return SampleBatch(
            batch_id=uuid4().hex,
            mode=self._delivery,
            wind_farm_id=self._site_id,
            collector_id=self.collector_id,
            task_id=task_id,
            device_type=device_type,
            device_id=device_id,
            point_table_id=point_table_id,
            device_config_id=device_config_id,
            sample_time=sample_time,
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
        task_id: str,
        device_config_id: str,
        sample_time: datetime,
        expected_point_ids: tuple[str, ...],
    ) -> list[SampleBatch]:
        if not samples:
            return []
        point_ids = [sample.point_id for sample in samples]
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("duplicate point_id in acquisition batch")
        device_type = validate_subject_token(device_type)
        task_id = validate_subject_token(task_id)
        if not point_table_id.startswith("point_table_") or "_v" not in point_table_id:
            raise ValueError("immutable point_table_id required")
        if not expected_point_ids or len(set(expected_point_ids)) != len(expected_point_ids):
            raise ValueError("complete physical PointTable point set required")
        if any(sample.point_id not in expected_point_ids for sample in samples):
            raise ValueError("acquired point is not in the immutable PointTable")
        by_point = {sample.point_id: self._item(sample, sample_time) for sample in samples}
        if self._delivery == "reliable":
            for point_id in expected_point_ids:
                if point_id not in by_point:
                    by_point[point_id] = SampleItem(
                        point_id=point_id,
                        sample_id=uuid4().hex,
                        value=None,
                        quality="not_collected",
                        sample_time=sample_time,
                    )
            items = tuple(by_point[point_id] for point_id in expected_point_ids)
        else:
            items = tuple(by_point[point_id] for point_id in by_point)
        batches: list[SampleBatch] = []
        if self._delivery == "reliable" and len(items) > self._max_samples:
            raise ValueError("reliable full device sample exceeds max_samples")
        start = 0
        while start < len(items):
            count = min(self._max_samples, len(items) - start)
            candidate = self._batch(
                device_id, task_id, device_type, point_table_id,
                device_config_id, sample_time, items[start : start + count],
            )
            while count:
                try:
                    candidate.encode(max_samples=self._max_samples, max_bytes=self._max_bytes)
                    break
                except ValueError:
                    if count == 1:
                        raise
                    count //= 2
                    if self._delivery == "reliable":
                        raise ValueError(
                            "reliable device sample exceeds transport bound; splitting forbidden"
                        )
                    candidate = self._batch(
                        device_id,
                        task_id,
                        device_type,
                        point_table_id,
                        device_config_id,
                        sample_time,
                        items[start : start + count],
                    )
            batches.append(candidate)
            start += count
        return batches
