"""IEC 60870-5-104 IOA (Information Object Address) codec — 3 bytes, little-endian."""

from __future__ import annotations

import struct

from wind_hub.domain.model.errors import ProtocolError

IOA_MAX = 0xFFFFFF  # 3 bytes max


def encode_ioa(ioa: int) -> bytes:
    """Encode an IOA as 3 bytes little-endian.

    Args:
        ioa: Information object address (0 .. 0xFFFFFF).

    Returns:
        3-byte ``bytes`` value.

    Raises:
        ProtocolError: If *ioa* is out of range.
    """
    if not (0 <= ioa <= IOA_MAX):
        raise ProtocolError(f"IOA {ioa:#x} out of range [0, {IOA_MAX:#x}]")
    # struct.pack('<I', ioa)[:3] — strip high byte
    return struct.pack("<I", ioa)[:3]


def decode_ioa(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode an IOA from *data* at *offset*.

    Args:
        data: Raw bytes buffer.
        offset: Start position.

    Returns:
        ``(ioa, new_offset)`` tuple.

    Raises:
        ProtocolError: If there are fewer than 3 bytes remaining.
    """
    if len(data) - offset < 3:
        raise ProtocolError(
            f"IOA decode: need 3 bytes at offset {offset}, " f"got {len(data) - offset}"
        )
    raw = data[offset : offset + 3]
    # Pad to 4 bytes for struct unpack
    ioa = struct.unpack("<I", raw + b"\x00")[0]
    return ioa, offset + 3
