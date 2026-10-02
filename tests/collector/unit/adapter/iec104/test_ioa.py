"""Tests for IOA codec."""

from __future__ import annotations

import pytest

from wind_hub_core.protocol.iec104.codec.ioa import decode_ioa, encode_ioa
from wind_hub_core.model.errors import ProtocolError


class TestEncodeIOA:
    def test_normal_value(self) -> None:
        assert encode_ioa(0x010203) == b"\x03\x02\x01"

    def test_zero(self) -> None:
        assert encode_ioa(0) == b"\x00\x00\x00"

    def test_max_value(self) -> None:
        assert encode_ioa(0xFFFFFF) == b"\xff\xff\xff"

    def test_power_of_two(self) -> None:
        assert encode_ioa(256) == b"\x00\x01\x00"

    def test_out_of_range_negative(self) -> None:
        with pytest.raises(ProtocolError, match="out of range"):
            encode_ioa(-1)

    def test_out_of_range_too_large(self) -> None:
        with pytest.raises(ProtocolError, match="out of range"):
            encode_ioa(0x1000000)


class TestDecodeIOA:
    def test_normal_value(self) -> None:
        ioa, off = decode_ioa(b"\x03\x02\x01")
        assert ioa == 0x010203
        assert off == 3

    def test_zero(self) -> None:
        ioa, off = decode_ioa(b"\x00\x00\x00")
        assert ioa == 0
        assert off == 3

    def test_insufficient_data(self) -> None:
        with pytest.raises(ProtocolError, match="need 3 bytes"):
            decode_ioa(b"\x01\x02")

    def test_with_offset(self) -> None:
        data = b"\xaa\xaa\x03\x02\x01\xbb"
        ioa, off = decode_ioa(data, offset=2)
        assert ioa == 0x010203
        assert off == 5


class TestIOARoundtrip:
    def test_roundtrip_zero(self) -> None:
        assert decode_ioa(encode_ioa(0))[0] == 0

    def test_roundtrip_max(self) -> None:
        assert decode_ioa(encode_ioa(0xFFFFFF))[0] == 0xFFFFFF

    def test_roundtrip_mid(self) -> None:
        assert decode_ioa(encode_ioa(16385))[0] == 16385
