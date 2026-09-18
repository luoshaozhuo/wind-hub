"""Unit tests for the DeadbandProcessor (stateful)."""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin.deadband import DeadbandProcessor
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.point import PointValue


def _point(point_id: str, *, deadband: float | None = 1.0) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        address=PointAddress(),
        data_type="float32",
        deadband=deadband,
    )


def _setup(points: list[PointConfig]) -> DeadbandProcessor:
    proc = DeadbandProcessor()
    proc.set_points_config({"d1": points})
    return proc


async def test_first_seen_point_is_emitted() -> None:
    proc = _setup([_point("p", deadband=1.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=10.0)])
    assert [o.value for o in out] == [10.0]


async def test_change_at_or_above_deadband_emits() -> None:
    proc = _setup([_point("p", deadband=1.0)])
    await proc.process([PointValue(device_id="d1", point_id="p", value=10.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=11.0)])
    assert [o.value for o in out] == [11.0]


async def test_change_below_deadband_is_filtered() -> None:
    proc = _setup([_point("p", deadband=1.0)])
    await proc.process([PointValue(device_id="d1", point_id="p", value=10.0)])
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=10.4)])
    assert out == []


async def test_point_without_deadband_always_emits() -> None:
    proc = _setup([_point("p", deadband=None)])
    for value in (1.0, 1.0, 1.0):
        out = await proc.process([PointValue(device_id="d1", point_id="p", value=value)])
        assert [o.value for o in out] == [value]


async def test_non_numeric_value_emits_unchanged() -> None:
    proc = _setup([_point("p", deadband=1.0)])
    pv = PointValue(device_id="d1", point_id="p", value=True)
    out = await proc.process([pv])
    assert out == [pv]


async def test_reset_clears_state() -> None:
    proc = _setup([_point("p", deadband=1.0)])
    await proc.process([PointValue(device_id="d1", point_id="p", value=10.0)])
    proc.reset()
    # After reset the next value is treated as first-seen and emitted.
    out = await proc.process([PointValue(device_id="d1", point_id="p", value=10.4)])
    assert [o.value for o in out] == [10.4]


async def test_multiple_points_keep_independent_state() -> None:
    proc = _setup([_point("a", deadband=1.0), _point("b", deadband=1.0)])
    await proc.process(
        [
            PointValue(device_id="d1", point_id="a", value=10.0),
            PointValue(device_id="d1", point_id="b", value=100.0),
        ]
    )
    # a changes within deadband (filtered), b changes beyond it (emitted).
    out = await proc.process(
        [
            PointValue(device_id="d1", point_id="a", value=10.4),
            PointValue(device_id="d1", point_id="b", value=101.5),
        ]
    )
    assert [(o.point_id, o.value) for o in out] == [("b", 101.5)]
