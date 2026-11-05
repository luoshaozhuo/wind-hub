"""Deterministic Modbus register-image semantics (no socket, no pymodbus server).

The TCP server framing is exercised against a real Modbus endpoint in Gate 4.
These tests pin the in-process rules that need no network: float32 big-endian
mapping, quality coils, out-of-order/duplicate/conflict handling, staleness
expiry and bounded reads.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta

import pytest

from contracts.sample_message import SampleMessage
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.realtime_adapters import _RegisterImage

pytestmark = [pytest.mark.unit, pytest.mark.modbus]

KEY = ("farm-a", "collector-1", "WT001", "active_power")
VALUE_START = 0
QUALITY_START = 100


def _sample(
    sample_id: str,
    *,
    value: float | int | bool | str | None = 35000.0,
    quality: str = "good",
    when: datetime | None = None,
    staleness: int | None = 5000,
) -> SampleMessage:
    now = datetime.now(UTC)
    return SampleMessage(
        wind_farm_id=KEY[0],
        collector_id=KEY[1],
        task_id="task-1",
        device_id=KEY[2],
        point_id=KEY[3],
        sample_id=sample_id,
        value=value,
        quality=quality,  # type: ignore[arg-type]
        sample_time=when or now,
        publish_time=now,
        max_staleness_ms=staleness,
    )


def _image() -> _RegisterImage:
    return _RegisterImage(points=(KEY,), value_start=VALUE_START, quality_start=QUALITY_START)


def test_duplicate_point_declaration_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="duplicate Modbus source point"):
        _RegisterImage(points=(KEY, KEY), value_start=0, quality_start=100)


def test_float32_value_and_quality_are_mapped_big_endian() -> None:
    image = _image()
    outcome = image.update((_sample("s1", value=35000.0, quality="good"),))
    assert outcome.delivered == (0,)
    assert outcome.settled == (0,)
    assert outcome.error == ""
    high, low = struct.unpack(">HH", struct.pack(">f", 35000.0))
    assert image.read(VALUE_START, 2) == [high, low]
    assert image.read(QUALITY_START, 1) == [0]  # good -> 0


def test_quality_codes_map_to_contract_values() -> None:
    image = _image()
    for quality, code in (("bad", 1), ("uncertain", 2)):
        image.update((_sample(f"q-{quality}", quality=quality),))
        assert image.read(QUALITY_START, 1) == [code]


def test_out_of_order_sample_never_rolls_back_the_image() -> None:
    image = _image()
    base = datetime.now(UTC) - timedelta(milliseconds=500)
    image.update((_sample("new", value=10.0, when=base),))
    older = _sample("old", value=999.0, when=base - timedelta(milliseconds=200))
    outcome = image.update((older,))
    assert outcome.delivered == ()  # not an output
    assert outcome.settled == (0,)
    high, low = struct.unpack(">HH", struct.pack(">f", 10.0))
    assert image.read(VALUE_START, 2) == [high, low]


def test_duplicate_is_confirmed_but_conflicting_identity_errors() -> None:
    image = _image()
    when = datetime.now(UTC) - timedelta(milliseconds=100)
    first = _sample("same", when=when)
    image.update((first,))
    duplicate = image.update((_sample("same", when=when),))
    assert duplicate.delivered == (0,) and duplicate.settled == (0,)

    conflict = image.update((_sample("different-id", when=when),))
    assert conflict.delivered == ()
    assert "conflicting identity" in conflict.error


def test_non_numeric_and_boolean_values_are_rejected() -> None:
    image = _image()
    outcome = image.update((_sample("bool", value=True),))
    assert outcome.delivered == ()
    assert "float32 requires numeric sample" in outcome.error


def test_stale_sample_is_settled_without_writing_the_image() -> None:
    image = _image()
    stale = _sample(
        "stale",
        when=datetime.now(UTC) - timedelta(seconds=10),
        staleness=1000,
    )
    outcome = image.update((stale,))
    assert outcome.delivered == ()
    assert outcome.settled == (0,)
    assert image.read(VALUE_START, 2) is None  # nothing was written


async def test_read_enforces_bounds_validity_and_expiry() -> None:
    import asyncio

    image = _image()
    assert image.read(VALUE_START, 2) is None  # never written
    image.update((_sample("s1", value=1.0, staleness=80),))
    assert image.read(VALUE_START, 2) is not None
    assert image.read(VALUE_START, 3) is None  # crosses into unmapped address
    assert image.read(VALUE_START - 1, 1) is None  # before the value block
    # A stale sample neither writes nor rolls back the currently valid image.
    stale = _sample(
        "stale",
        value=2.0,
        when=datetime.now(UTC) - timedelta(seconds=1),
        staleness=100,
    )
    stale_outcome = image.update((stale,))
    assert stale_outcome.delivered == ()
    assert image.read(VALUE_START, 2) is not None
    # The register becomes unavailable only once its own staleness clock expires.
    await asyncio.sleep(0.1)
    assert image.read(VALUE_START, 2) is None


def test_unmapped_point_is_reported_but_does_not_abort_the_batch() -> None:
    image = _RegisterImage(points=(KEY,), value_start=0, quality_start=100)
    # Build an unmapped sample explicitly rather than mutating the frozen model.
    now = datetime.now(UTC)
    unmapped = SampleMessage(
        wind_farm_id=KEY[0],
        collector_id=KEY[1],
        task_id="task-1",
        device_id="WT999",
        point_id=KEY[3],
        sample_id="x",
        value=1.0,
        quality="good",
        sample_time=now,
        publish_time=now,
        max_staleness_ms=5000,
    )
    outcome = image.update((unmapped,))
    assert outcome.delivered == ()
    assert "Modbus point not mapped" in outcome.error
