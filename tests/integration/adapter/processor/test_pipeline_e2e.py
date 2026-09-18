"""Integration test — full Processor pipeline (quality_check → unit_convert → deadband).

Verifies the three builtin processors wired together via :class:`Pipeline`,
including per-point config injection, the order of operations, and the
statefulness of deadband across batches.
"""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin.deadband import DeadbandProcessor
from wind_hub.adapter.outbound.processor.builtin.quality_check import QualityCheckProcessor
from wind_hub.adapter.outbound.processor.builtin.unit_convert import UnitConvertProcessor
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.processing.pipeline import Pipeline


def _points() -> list[PointConfig]:
    return [
        PointConfig(
            point_id="meas",
            device_id="d1",
            address=PointAddress(),
            data_type="float32",
            scale=10.0,
            offset=0.0,
            deadband=5.0,
            min_value=0.0,
            max_value=100.0,
        ),
    ]


def _build(*names: str) -> Pipeline:
    """Build a pipeline of the named builtin processors, with points injected."""
    registry = {
        "quality_check": QualityCheckProcessor,
        "unit_convert": UnitConvertProcessor,
        "deadband": DeadbandProcessor,
    }
    processors = [registry[n]() for n in names]
    for proc in processors:
        proc.set_points_config(_points())
    return Pipeline(processors)


async def test_full_chain_converts_filters_and_validates() -> None:
    pipeline = _build("quality_check", "unit_convert", "deadband")

    # First batch: raw 7.0 in range → GOOD, ×10 → 70.0, first-seen → emitted.
    first = await pipeline.process([PointValue(device_id="d1", point_id="meas", value=7.0)])
    assert [(o.value, o.quality) for o in first] == [(70.0, Quality.GOOD)]

    # Second batch: raw 7.1 → ×10 = 71.0, |71-70|=1 < deadband 5 → filtered.
    second = await pipeline.process([PointValue(device_id="d1", point_id="meas", value=7.1)])
    assert second == []

    # Third batch: raw 8.0 → ×10 = 80.0, |80-70|=10 ≥ 5 → emitted.
    third = await pipeline.process([PointValue(device_id="d1", point_id="meas", value=8.0)])
    assert [(o.value, o.quality) for o in third] == [(80.0, Quality.GOOD)]


async def test_quality_check_marks_bad_but_convert_still_applies() -> None:
    pipeline = _build("quality_check", "unit_convert", "deadband")

    # raw 150.0 exceeds max_value 100 → quality BAD; unit_convert still applies ×10.
    out = await pipeline.process([PointValue(device_id="d1", point_id="meas", value=150.0)])
    assert [(o.value, o.quality) for o in out] == [(1500.0, Quality.BAD)]


async def test_processor_order_affects_result() -> None:
    # raw 50.0 is within raw range [0, 100] but after ×10 becomes 500.0.
    #   quality_check → unit_convert: validated raw (good), then ×10 → 500.0 GOOD.
    #   unit_convert → quality_check: ×10 → 500.0, then validated against [0,100] → BAD.
    qc_first = _build("quality_check", "unit_convert")
    uc_first = _build("unit_convert", "quality_check")

    a = await qc_first.process([PointValue(device_id="d1", point_id="meas", value=50.0)])
    b = await uc_first.process([PointValue(device_id="d1", point_id="meas", value=50.0)])

    assert a[0].quality is Quality.GOOD
    assert b[0].quality is Quality.BAD
    assert a[0].value == b[0].value == 500.0
