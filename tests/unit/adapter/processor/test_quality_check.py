"""Unit tests for the QualityCheckProcessor (range validation)."""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin.quality_check import QualityCheckProcessor
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.point import PointValue, Quality


def _point(
    point_id: str,
    *,
    device_id: str = "d1",
    min_value: float | None = None,
    max_value: float | None = None,
    data_type: str = "float32",
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id=device_id,
        address=PointAddress(),
        data_type=data_type,
        min_value=min_value,
        max_value=max_value,
    )


def _setup(points: list[PointConfig]) -> QualityCheckProcessor:
    proc = QualityCheckProcessor()
    proc.set_points_config(points)
    return proc


async def test_in_range_value_keeps_quality() -> None:
    proc = _setup([_point("p", min_value=0.0, max_value=100.0)])
    pv = PointValue(device_id="d1", point_id="p", value=50.0)
    out = await proc.process([pv])
    assert out[0].value == 50.0
    assert out[0].quality is Quality.GOOD


async def test_below_min_marks_bad() -> None:
    proc = _setup([_point("p", min_value=0.0, max_value=100.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=-5.0)])
    assert out[0].quality is Quality.BAD
    assert out[0].value == -5.0


async def test_above_max_marks_bad() -> None:
    proc = _setup([_point("p", min_value=0.0, max_value=100.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=150.0)])
    assert out[0].quality is Quality.BAD
    assert out[0].value == 150.0


async def test_no_bounds_means_no_validation() -> None:
    proc = _setup([_point("p", min_value=None, max_value=None)])
    for value in (-999.0, 0.0, 999.0):
        out = await proc.process([PointValue(device_id="d1", point_id="p", value=value)])
        assert out[0].quality is Quality.GOOD


async def test_non_numeric_value_passes_through() -> None:
    proc = _setup([_point("p", min_value=0.0, max_value=100.0)])
    pv = PointValue(device_id="d1", point_id="p", value="abc")
    out = await proc.process([pv])
    assert out == [pv]


async def test_existing_uncertain_quality_unchanged() -> None:
    proc = _setup([_point("p", min_value=0.0, max_value=100.0)])
    pv = PointValue(device_id="d1", point_id="p", value=50.0, quality=Quality.UNCERTAIN)
    out = await proc.process([pv])
    assert out[0].quality is Quality.UNCERTAIN


async def test_one_sided_bound() -> None:
    # 只配置下限：高于下限不校验。
    proc = _setup([_point("p", min_value=0.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=1e9)])
    assert out[0].quality is Quality.GOOD
