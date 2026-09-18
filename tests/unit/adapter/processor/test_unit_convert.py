"""Unit tests for the UnitConvertProcessor real implementation."""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin.unit_convert import UnitConvertProcessor
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.point import PointValue, Quality


def _point(
    point_id: str,
    *,
    data_type: str = "float32",
    scale: float = 1.0,
    offset: float = 0.0,
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        address=PointAddress(),
        data_type=data_type,
        scale=scale,
        offset=offset,
    )


def _setup(points: list[PointConfig]) -> UnitConvertProcessor:
    proc = UnitConvertProcessor()
    proc.set_points_config({"d1": points})
    return proc


async def test_identity_scale_offset_returns_unchanged() -> None:
    proc = _setup([_point("p", scale=1.0, offset=0.0)])
    pv = PointValue(device_id="d1", point_id="p", value=42.0)
    out = await proc.process([pv])
    assert out == [pv]
    assert out[0].value == 42.0


async def test_scale_multiplies_value() -> None:
    proc = _setup([_point("p", scale=0.1)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=1500.5)])
    assert out[0].value == 150.05


async def test_offset_adds_to_value() -> None:
    proc = _setup([_point("p", offset=10.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=100.0)])
    assert out[0].value == 110.0


async def test_scale_and_offset_together() -> None:
    proc = _setup([_point("p", scale=2.0, offset=10.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=3.0)])
    assert out[0].value == 16.0


async def test_non_numeric_data_type_passes_through() -> None:
    proc = _setup([_point("p", data_type="str")])
    pv = PointValue(device_id="d1", point_id="p", value="hello")
    out = await proc.process([pv])
    assert out[0].value == "hello"
    assert out[0].quality is Quality.GOOD


async def test_unconfigured_point_passes_through() -> None:
    proc = _setup([_point("known", scale=0.1)])
    pv = PointValue(device_id="d1", point_id="unknown", value=99.0)
    out = await proc.process([pv])
    assert out == [pv]
    assert out[0].value == 99.0


async def test_non_numeric_value_marks_bad() -> None:
    # 数值类型点但运行时值类型不匹配（如字符串）→ quality=BAD，值不变。
    proc = _setup([_point("p", scale=0.1)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value="oops")])
    assert out[0].value == "oops"
    assert out[0].quality is Quality.BAD


async def test_empty_batch_returns_empty() -> None:
    proc = _setup([_point("p", scale=0.1)])
    assert await proc.process([]) == []


async def test_name_is_unit_convert() -> None:
    assert UnitConvertProcessor().name == "unit_convert"
