"""Unit tests for the Pipeline component."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wind_hub.domain.model.point import PointValue
from wind_hub.domain.processing.pipeline import Pipeline

_FROZEN_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _make_pv(point_id: str, device_id: str = "d1", value: float = 1.0) -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        timestamp=_FROZEN_TS,
    )


# ---------------------------------------------------------------------------
# 1. Empty pipeline — data passes through unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_pipeline_passthrough() -> None:
    pipeline = Pipeline([])
    batch = [_make_pv("p1"), _make_pv("p2")]
    result = await pipeline.process(batch)
    assert result == batch
    assert pipeline.processor_count == 0


# ---------------------------------------------------------------------------
# 2. Single processor — data is transformed
# ---------------------------------------------------------------------------


class _ScaleProcessor:
    """Multiplies every value by 2."""

    @property
    def name(self) -> str:
        return "scaler"

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        for pv in batch:
            pv.value = pv.value * 2
        return batch


@pytest.mark.asyncio
async def test_single_processor_transforms() -> None:
    pipeline = Pipeline([_ScaleProcessor()])
    batch = [_make_pv("p1", value=5.0)]
    result = await pipeline.process(batch)
    assert result[0].value == 10.0
    assert pipeline.processor_count == 1


# ---------------------------------------------------------------------------
# 3. Multiple processors — executed in order
# ---------------------------------------------------------------------------


class _AppendProcessor:
    """Appends a derived point to the batch."""

    def __init__(self, suffix: str) -> None:
        self._suffix = suffix

    @property
    def name(self) -> str:
        return f"append_{self._suffix}"

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        return batch + [
            PointValue(
                device_id="d1",
                point_id=f"derived.{self._suffix}",
                value=99.0,
                timestamp=_FROZEN_TS,
            )
        ]


@pytest.mark.asyncio
async def test_multiple_processors_in_order() -> None:
    pipeline = Pipeline(
        [
            _ScaleProcessor(),
            _AppendProcessor("a"),
        ]
    )
    batch = [_make_pv("p1", value=5.0)]
    result = await pipeline.process(batch)
    # scaler: 5.0 → 10.0, then append: adds derived.a
    assert len(result) == 2
    assert result[0].value == 10.0  # scaled
    assert result[1].point_id == "derived.a"  # appended
    assert pipeline.processor_count == 2


# ---------------------------------------------------------------------------
# 4. Processor failure — skipped, batch continues
# ---------------------------------------------------------------------------


class _FailingProcessor:
    """Always raises on process."""

    @property
    def name(self) -> str:
        return "failing"

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        raise RuntimeError("simulated failure")


@pytest.mark.asyncio
async def test_failing_processor_skipped() -> None:
    pipeline = Pipeline([_FailingProcessor(), _ScaleProcessor()])
    batch = [_make_pv("p1", value=3.0)]
    result = await pipeline.process(batch)
    # failing processor skipped, scaler still runs
    assert result == batch
    assert result[0].value == 6.0  # scaled (failing output = original batch)
    assert pipeline.processor_count == 2


# ---------------------------------------------------------------------------
# 5. Failing processor — subsequent still execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsequent_processors_run_after_failure() -> None:
    pipeline = Pipeline(
        [
            _FailingProcessor(),
            _AppendProcessor("post"),
        ]
    )
    batch = [_make_pv("p1")]
    result = await pipeline.process(batch)
    # failing skipped, append still runs on original batch
    assert len(result) == 2
    assert result[1].point_id == "derived.post"
    assert pipeline.processor_count == 2


# ---------------------------------------------------------------------------
# 6. processor_count property
# ---------------------------------------------------------------------------


def test_processor_count_empty() -> None:
    assert Pipeline([]).processor_count == 0


def test_processor_count_three() -> None:
    assert (
        Pipeline(
            [
                _ScaleProcessor(),
                _ScaleProcessor(),
                _ScaleProcessor(),
            ]
        ).processor_count
        == 3
    )
