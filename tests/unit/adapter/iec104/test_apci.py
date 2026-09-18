"""Tests for APCI (I/S/U frame) encode/decode."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.iec104.codec.apci import (
    IFrame,
    SFrame,
    UFrame,
    decode_apdu,
    encode_apdu,
    encode_i_frame,
    encode_s_frame,
    encode_u_frame,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import UFrameType
from wind_hub.domain.model.errors import ProtocolError

# ==========================================================================
# I-frame
# ==========================================================================


class TestIFrame:
    def test_encode(self) -> None:
        asdu = b"\x01\x02\x03\x04"  # 4-byte dummy ASDU
        data = encode_i_frame(send_seq=5, recv_seq=10, asdu=asdu)
        # Wire: 0x68 | len | ctrl(4) | asdu(4)
        # len = 4 + 4 = 8
        assert data[0] == 0x68
        assert data[1] == 8  # length of ctrl + asdu
        # send_seq=5 → (5<<1)=10=0x000A → data[2:4]
        # recv_seq=10 → (10<<1)=20=0x0014 → data[4:6]
        assert data[6:10] == asdu
        assert len(data) == 2 + 4 + 4  # start + len + ctrl(4) + asdu(4)

    def test_decode(self) -> None:
        asdu = b"\x0a\x0b"
        # 0x68 | len=6 | ctrl: (5<<1)(10<<1) | asdu
        data = bytes([0x68, 0x06]) + bytes([0x0A, 0x00, 0x14, 0x00]) + asdu
        frame = decode_apdu(data)
        assert isinstance(frame, IFrame)
        assert frame.send_seq == 5  # (0x000A >> 1)
        assert frame.recv_seq == 10  # (0x0014 >> 1)
        assert frame.asdu == asdu

    def test_roundtrip(self) -> None:
        asdu = bytes(range(10))
        original = IFrame(send_seq=100, recv_seq=200, asdu=asdu)
        data = encode_apdu(original)
        decoded = decode_apdu(data)
        assert isinstance(decoded, IFrame)
        assert decoded.send_seq == 100
        assert decoded.recv_seq == 200
        assert decoded.asdu == asdu

    def test_sequence_boundary(self) -> None:
        data = encode_i_frame(send_seq=32767, recv_seq=32767, asdu=b"")
        frame = decode_apdu(data)
        assert isinstance(frame, IFrame)
        assert frame.send_seq == 32767
        assert frame.recv_seq == 32767

    def test_sequence_out_of_range(self) -> None:
        with pytest.raises(ProtocolError, match="out of range"):
            encode_i_frame(send_seq=32768, recv_seq=0, asdu=b"")


# ==========================================================================
# S-frame
# ==========================================================================


class TestSFrame:
    def test_encode(self) -> None:
        data = encode_s_frame(recv_seq=42)
        assert data[0] == 0x68
        assert data[1] == 4  # ctrl only, 4 bytes
        assert len(data) == 2 + 4  # start + len + ctrl(4)

    def test_decode(self) -> None:
        # recv_seq=42 → (42<<1)=84=0x0054
        data = bytes([0x68, 0x04, 0x01, 0x00, 0x54, 0x00])
        frame = decode_apdu(data)
        assert isinstance(frame, SFrame)
        assert frame.recv_seq == 42

    def test_roundtrip(self) -> None:
        original = SFrame(recv_seq=5555)
        data = encode_apdu(original)
        decoded = decode_apdu(data)
        assert isinstance(decoded, SFrame)
        assert decoded.recv_seq == 5555


# ==========================================================================
# U-frame
# ==========================================================================


class TestUFrame:
    @pytest.mark.parametrize("ft", list(UFrameType))
    def test_encode_decode(self, ft: UFrameType) -> None:
        data = encode_u_frame(ft)
        assert data[0] == 0x68
        assert len(data) == 6  # start + len + 4-byte ctrl
        frame = decode_apdu(data)
        assert isinstance(frame, UFrame)
        assert frame.frame_type == ft

    def test_roundtrip(self) -> None:
        for ft in UFrameType:
            original = UFrame(frame_type=ft)
            data = encode_apdu(original)
            decoded = decode_apdu(data)
            assert isinstance(decoded, UFrame)
            assert decoded.frame_type == ft


# ==========================================================================
# Error cases
# ==========================================================================


class TestAPCIErrors:
    def test_bad_start_byte(self) -> None:
        data = bytes([0x69, 0x04, 0x01, 0x00, 0x00, 0x00])
        with pytest.raises(ProtocolError, match="expected start byte"):
            decode_apdu(data)

    def test_too_short(self) -> None:
        data = bytes([0x68])
        with pytest.raises(ProtocolError, match="need at least 2"):
            decode_apdu(data)

    def test_apdu_len_too_small(self) -> None:
        data = bytes([0x68, 0x03, 0x01, 0x00, 0x00])
        with pytest.raises(ProtocolError, match="too small"):
            decode_apdu(data)

    def test_truncated_frame(self) -> None:
        # apdu_len says 6 bytes after length, but we only have 4
        data = bytes([0x68, 0x06, 0x01, 0x02, 0x03])
        with pytest.raises(ProtocolError, match="got"):
            decode_apdu(data)

    def test_unknown_u_frame_type(self) -> None:
        data = bytes([0x68, 0x04, 0xFF, 0x00, 0x00, 0x00])
        with pytest.raises(ProtocolError, match="Unknown U-frame"):
            decode_apdu(data)
