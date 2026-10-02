"""IEC 60870-5-104 IOA 的 3 字节 little-endian codec。"""

from __future__ import annotations

import struct

from wind_hub_core.model.errors import ProtocolError

IOA_MAX = 0xFFFFFF  # 3 bytes max


def encode_ioa(ioa: int) -> bytes:
    """把 IOA 编码为 3 字节 little-endian。

    Args:
        ioa: Information Object Address，范围 0~0xFFFFFF。

    Returns:
        3 字节编码结果。

    Raises:
        ProtocolError: IOA 越界。
    """
    if not (0 <= ioa <= IOA_MAX):
        raise ProtocolError(f"IOA {ioa:#x} out of range [0, {IOA_MAX:#x}]")
    # 先按 uint32 little-endian 编码，再去掉最高字节。
    return struct.pack("<I", ioa)[:3]


def decode_ioa(data: bytes, offset: int = 0) -> tuple[int, int]:
    """从指定 offset 解码 3 字节 IOA。

    Args:
        data: 原始字节缓冲区。
        offset: IOA 起始偏移。

    Returns:
        (ioa, new_offset)。

    Raises:
        ProtocolError: 剩余字节不足 3 个。
    """
    if len(data) - offset < 3:
        raise ProtocolError(
            f"IOA decode: need 3 bytes at offset {offset}, " f"got {len(data) - offset}"
        )
    raw = data[offset : offset + 3]
    # 补一个高位 0，再按 uint32 little-endian 解码。
    ioa = struct.unpack("<I", raw + b"\x00")[0]
    return ioa, offset + 3
