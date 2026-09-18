"""IEC 60870-5-104 APCI (Application Protocol Control Information) codec.

Handles I-frame, S-frame, and U-frame encode/decode.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from wind_hub.adapter.outbound.protocol.iec104.codec.types import UFrameType
from wind_hub.domain.model.errors import ProtocolError

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

START_CHAR = 0x68
"""IEC 60870-5-104 start character — always 0x68."""

MAX_SEQ = 0x7FFF
"""Maximum sequence number (15 bits)."""

# ---------------------------------------------------------------------------
# frame dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IFrame:
    """I-frame — information transfer frame.

    Carries an ASDU payload with send/recv sequence numbers for
    flow-control and acknowledgement.
    """

    send_seq: int
    """N(S) — send sequence number (0–32767)."""

    recv_seq: int
    """N(R) — receive sequence number (0–32767)."""

    asdu: bytes
    """Raw ASDU payload bytes."""


@dataclass(frozen=True)
class SFrame:
    """S-frame — supervisory / acknowledgement-only frame.

    Contains only a receive sequence number N(R); no ASDU payload.
    """

    recv_seq: int
    """N(R) — receive sequence number (0–32767)."""


@dataclass(frozen=True)
class UFrame:
    """U-frame — unnumbered control frame.

    Carries STARTDT / STOPDT / TESTFR commands without sequence numbers.
    """

    frame_type: UFrameType
    """U-frame function code."""


# APDUFrame is the sum-type for all three frame kinds.
APDUFrame = IFrame | SFrame | UFrame


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------


def decode_apdu(data: bytes) -> APDUFrame:
    """Decode a complete APDU from *data*.

    Wire format::

        0x68 | len | ctrl[0:4] | [asdu ...]

    where *len* = 4 (ctrl bytes) + len(asdu).

    Returns one of ``IFrame``, ``SFrame``, or ``UFrame``.

    Raises:
        ProtocolError: On structural errors.
    """
    if len(data) < 2:
        raise ProtocolError(f"APDU decode: need at least 2 bytes, got {len(data)}")
    if data[0] != START_CHAR:
        raise ProtocolError(
            f"APDU decode: expected start byte {START_CHAR:#04x}, " f"got {data[0]:#04x}"
        )

    apdu_len = data[1]  # number of bytes for ctrl(4) + asdu(N)
    total_len = 2 + apdu_len
    if len(data) < total_len:
        raise ProtocolError(
            f"APDU decode: need {total_len} bytes (apdu_len={apdu_len}), " f"got {len(data)}"
        )

    if apdu_len < 4:
        raise ProtocolError(f"APDU decode: apdu_len={apdu_len} too small (min 4 for ctrl)")

    # Control field bytes (4 bytes at offset 2)
    ctrl = data[2:6]

    # Determine frame type from control field bit 0
    if (ctrl[0] & 0x01) == 0:
        # I-frame — bit 0 = 0
        send_seq_raw = struct.unpack_from("<H", ctrl, 0)[0]
        recv_seq_raw = struct.unpack_from("<H", ctrl, 2)[0]
        send_seq = (send_seq_raw >> 1) & MAX_SEQ
        recv_seq = (recv_seq_raw >> 1) & MAX_SEQ
        asdu = data[6:total_len]
        return IFrame(send_seq=send_seq, recv_seq=recv_seq, asdu=asdu)

    elif (ctrl[0] & 0x03) == 0x01:
        # S-frame — bit 0 = 1, bit 1 = 0
        recv_seq_raw = struct.unpack_from("<H", ctrl, 2)[0]
        recv_seq = (recv_seq_raw >> 1) & MAX_SEQ
        return SFrame(recv_seq=recv_seq)

    elif (ctrl[0] & 0x03) == 0x03:
        # U-frame — bit 0 = 1, bit 1 = 1
        func_code = ctrl[0]
        try:
            frame_type = UFrameType(func_code)
        except ValueError:
            raise ProtocolError(f"Unknown U-frame function code: {func_code:#04x}") from None
        return UFrame(frame_type=frame_type)

    else:
        raise ProtocolError(f"APDU decode: unrecognized frame type, control[0]={ctrl[0]:#04x}")


# ---------------------------------------------------------------------------
# encode helpers
# ---------------------------------------------------------------------------


def _validate_seq(n: int, label: str) -> None:
    """Raise ProtocolError if sequence number is out of range."""
    if not (0 <= n <= MAX_SEQ):
        raise ProtocolError(f"{label} {n} out of range [0, {MAX_SEQ}]")


def _build_apdu(body: bytes) -> bytes:
    """Build a complete APDU: 0x68 | len | body (ctrl[4] + asdu)."""
    length = len(body)
    return bytes([START_CHAR, length]) + body


def encode_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    """Encode an I-frame.

    Args:
        send_seq: N(S), 15-bit send sequence number.
        recv_seq: N(R), 15-bit receive sequence number.
        asdu: Raw ASDU bytes.

    Returns:
        Complete APDU bytes (including 0x68 header).

    Raises:
        ProtocolError: If sequence numbers are out of range.
    """
    _validate_seq(send_seq, "send_seq")
    _validate_seq(recv_seq, "recv_seq")

    # Control field (4 bytes):
    #   bytes 0-1: send_seq << 1  (bit 0 = 0 for I-frame)
    #   bytes 2-3: recv_seq << 1
    ctrl = struct.pack("<HH", (send_seq << 1) & 0xFFFF, (recv_seq << 1) & 0xFFFF)
    return _build_apdu(ctrl + asdu)


def encode_s_frame(recv_seq: int) -> bytes:
    """Encode an S-frame (acknowledgement only).

    Args:
        recv_seq: N(R), 15-bit receive sequence number.

    Returns:
        Complete APDU bytes (always 2 + 4 = 6 bytes).

    Raises:
        ProtocolError: If sequence number is out of range.
    """
    _validate_seq(recv_seq, "recv_seq")

    # S-frame control field (4 bytes):
    #   bytes 0-1: 0x0001 (S-frame marker: bit 0 = 1, bit 1 = 0)
    #   bytes 2-3: recv_seq << 1
    ctrl = struct.pack("<H", 0x0001)  # bits 0-1 = 01
    ctrl += struct.pack("<H", (recv_seq << 1) & 0xFFFF)
    return _build_apdu(ctrl)


def encode_u_frame(frame_type: UFrameType) -> bytes:
    """Encode a U-frame.

    Args:
        frame_type: One of the ``UFrameType`` enum values.

    Returns:
        Complete APDU bytes (always 2 + 4 = 6 bytes).

    Raises:
        ProtocolError: If *frame_type* is not a valid ``UFrameType``.
    """
    ctrl = bytes([frame_type.value, 0x00, 0x00, 0x00])
    return _build_apdu(ctrl)


def encode_apdu(frame: APDUFrame) -> bytes:
    """Encode any of I/S/U frame to an APDU.

    Args:
        frame: An ``IFrame``, ``SFrame``, or ``UFrame``.

    Returns:
        Complete APDU bytes.
    """
    if isinstance(frame, UFrame):
        return encode_u_frame(frame.frame_type)
    elif isinstance(frame, SFrame):
        return encode_s_frame(frame.recv_seq)
    elif isinstance(frame, IFrame):
        return encode_i_frame(frame.send_seq, frame.recv_seq, frame.asdu)
    else:
        raise ProtocolError(f"Unknown frame type: {type(frame).__name__}")
