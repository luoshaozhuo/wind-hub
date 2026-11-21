"""Application policy for immutable Collector sample and batch identities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from contracts.sample_batch import SampleBatch, SampleItem, validate_subject_token
from core.application import ProtocolSample, Quality
from core.domain import BusinessPoint, PointTable, convert_value, engineering_value


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

    def _item(
        self,
        sample: ProtocolSample,
        point_table: PointTable,
        business_points: Mapping[str, BusinessPoint],
    ) -> SampleItem:
        point = point_table.points.get(sample.point_id)
        if point is None:
            raise ValueError(f"unknown point_id in acquisition batch: {sample.point_id}")
        business_point = business_points.get(str(point.business_point_id))
        if business_point is None:
            raise ValueError(f"missing BusinessPoint closure: {point.business_point_id}")
        value: float | int | bool | str | None
        business_value: float | int | bool | str | None
        try:
            value = engineering_value(point, sample.value)
            if value is None or isinstance(value, bool | str):
                business_value = value
            else:
                converted = convert_value(value, point.source_unit, business_point.standard_unit)
                business_value = business_point.coerce(converted)
        except (ValueError, TypeError, OverflowError):
            if sample.quality is not Quality.BAD:
                raise
            # Acquisition already marked an unconvertible reading BAD. Only RAW
            # retains the decoded scalar; failed transforms stay explicitly absent.
            value = None
            business_value = None
        return SampleItem(
            point_id=sample.point_id,
            sample_id=uuid4().hex,
            value=value,
            raw_value=sample.value,
            business_value=business_value,
            unit=point.source_unit.unit_id,
            business_unit=business_point.standard_unit.unit_id,
            business_point_id=str(point.business_point_id),
            quality=sample.quality.value,
            sample_time=sample.timestamp,
            receive_time=sample.receive_time,
            max_staleness_ms=self._staleness if self._delivery == "realtime" else None,
        )

    def _batch(
        self,
        device_id: str,
        samples: tuple[SampleItem, ...],
        *,
        device_type: str,
        point_table_id: str,
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
        point_table: PointTable,
        business_points: Mapping[str, BusinessPoint],
    ) -> list[SampleBatch]:
        """Build v3 batches from decoded values and the effective versioned plan."""
        if not samples:
            return []
        if not device_type or not point_table_id.startswith("point_table_"):
            raise ValueError("complete device and versioned PointTable identity required")
        point_ids = [sample.point_id for sample in samples]
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("duplicate point_id in acquisition batch")
        items = tuple(self._item(sample, point_table, business_points) for sample in samples)
        batches: list[SampleBatch] = []
        start = 0
        while start < len(items):
            count = min(self._max_samples, len(items) - start)
            candidate = self._batch(
                device_id,
                items[start : start + count],
                device_type=device_type,
                point_table_id=point_table_id,
            )
            while count:
                try:
                    candidate.encode(max_samples=self._max_samples, max_bytes=self._max_bytes)
                    break
                except ValueError:
                    if count == 1:
                        raise
                    count //= 2
                    candidate = self._batch(
                        device_id,
                        items[start : start + count],
                        device_type=device_type,
                        point_table_id=point_table_id,
                    )
            batches.append(candidate)
            start += count
        return batches
