"""Canonical two-bit quality bitmap tests for wide-table schemas."""

import pytest

from storage_service.domain.quality_bitmap import (
    PointQuality,
    pack_quality_bitmap,
    unpack_quality_bitmap,
)


def test_low_bit_first_and_cross_byte_boundary() -> None:
    values = [0, 1, 2, 3, 3, 2, 1, 0, 1]
    encoded = pack_quality_bitmap(values)
    assert encoded == bytes([0b11100100, 0b00011011, 0b00000001])
    assert unpack_quality_bitmap(encoded, len(values)) == tuple(PointQuality(v) for v in values)


def test_five_hundred_fields_occupy_exactly_125_bytes() -> None:
    values = [index % 4 for index in range(500)]
    encoded = pack_quality_bitmap(values)
    assert len(encoded) == 125
    assert unpack_quality_bitmap(encoded, 500) == tuple(PointQuality(v) for v in values)


@pytest.mark.parametrize("values", [[-1], [4], [True], [1.0], ["1"]])
def test_invalid_quality_is_rejected(values: list[object]) -> None:
    with pytest.raises(ValueError, match="invalid quality"):
        pack_quality_bitmap(values)  # type: ignore[arg-type]


def test_empty_bitmap_and_unused_padding() -> None:
    assert pack_quality_bitmap([]) == b""
    assert unpack_quality_bitmap(b"", 0) == ()
    with pytest.raises(ValueError, match="length"):
        unpack_quality_bitmap(b"\x00", 0)
    with pytest.raises(ValueError, match="padding"):
        unpack_quality_bitmap(b"\x41", 1)
