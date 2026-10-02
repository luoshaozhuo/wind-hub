"""IEC 60870-5-104 APCI 编码与解码。

支持 I-frame、S-frame、U-frame。该模块只做字节转换和结构校验，不维护连接
序号状态；序号有效范围为 15 bit，结构或序号非法时抛 ProtocolError。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from wind_hub.adapter.outbound.protocol.iec104.codec.types import UFrameType
from wind_hub.domain.model.errors import ProtocolError

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

START_CHAR = 0x68
"""IEC104 固定起始字符 0x68。"""

MAX_SEQ = 0x7FFF
"""15-bit 序号最大值。"""

# ---------------------------------------------------------------------------
# APDU frame 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IFrame:
    """I-frame 信息传输帧。

    携带 ASDU，同时通过 N(S)/N(R) 完成发送序号和接收确认。
    """

    send_seq: int
    """N(S) 发送序号，范围 0~32767。"""

    recv_seq: int
    """N(R) 接收确认序号，范围 0~32767。"""

    asdu: bytes
    """原始 ASDU payload。"""


@dataclass(frozen=True)
class SFrame:
    """S-frame 监督确认帧。

    只携带 N(R)，不包含 ASDU。
    """

    recv_seq: int
    """N(R) — receive sequence number (0–32767)."""


@dataclass(frozen=True)
class UFrame:
    """U-frame 无编号控制帧。

    用于 STARTDT、STOPDT、TESTFR 等链路控制，不携带序号。
    """

    frame_type: UFrameType
    """U-frame 功能码。"""


# APDUFrame 是三种 frame 的联合类型。
APDUFrame = IFrame | SFrame | UFrame


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------


def decode_apdu(data: bytes) -> APDUFrame:
    """从完整字节序列解码一个 APDU。

    Args:
        data: 从 0x68 起始字符开始的完整 APDU 字节。

    Returns:
        IFrame、SFrame 或 UFrame。

    Raises:
        ProtocolError: 起始字符、长度、控制域或 U-frame 功能码非法。
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

    # offset=2 起的 4 字节控制域。
    ctrl = data[2:6]

    # 根据控制域低位判断 frame 类型。
    if (ctrl[0] & 0x01) == 0:
        # I-frame：bit0=0。
        send_seq_raw = struct.unpack_from("<H", ctrl, 0)[0]
        recv_seq_raw = struct.unpack_from("<H", ctrl, 2)[0]
        send_seq = (send_seq_raw >> 1) & MAX_SEQ
        recv_seq = (recv_seq_raw >> 1) & MAX_SEQ
        asdu = data[6:total_len]
        return IFrame(send_seq=send_seq, recv_seq=recv_seq, asdu=asdu)

    elif (ctrl[0] & 0x03) == 0x01:
        # S-frame：bit0=1、bit1=0。
        recv_seq_raw = struct.unpack_from("<H", ctrl, 2)[0]
        recv_seq = (recv_seq_raw >> 1) & MAX_SEQ
        return SFrame(recv_seq=recv_seq)

    elif (ctrl[0] & 0x03) == 0x03:
        # U-frame：bit0=1、bit1=1。
        func_code = ctrl[0]
        try:
            frame_type = UFrameType(func_code)
        except ValueError:
            raise ProtocolError(f"Unknown U-frame function code: {func_code:#04x}") from None
        return UFrame(frame_type=frame_type)

    else:
        raise ProtocolError(f"APDU decode: unrecognized frame type, control[0]={ctrl[0]:#04x}")


# ---------------------------------------------------------------------------
# 编码辅助函数
# ---------------------------------------------------------------------------


def _validate_seq(n: int, label: str) -> None:
    """校验 15-bit 序号范围；越界时抛 ProtocolError。"""
    if not (0 <= n <= MAX_SEQ):
        raise ProtocolError(f"{label} {n} out of range [0, {MAX_SEQ}]")


def _build_apdu(body: bytes) -> bytes:
    """按 0x68 | len | body 组装完整 APDU。"""
    length = len(body)
    return bytes([START_CHAR, length]) + body


def encode_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    """编码 I-frame。

    Args:
        send_seq: N(S) 发送序号。
        recv_seq: N(R) 接收确认序号。
        asdu: 原始 ASDU 字节。

    Returns:
        含 APCI 头的完整 APDU。

    Raises:
        ProtocolError: 任一序号越界。
    """
    _validate_seq(send_seq, "send_seq")
    _validate_seq(recv_seq, "recv_seq")

    # I-frame 4 字节控制域：
    #   bytes 0-1: send_seq << 1  (bit 0 = 0 for I-frame)
    #   bytes 2-3: recv_seq << 1
    ctrl = struct.pack("<HH", (send_seq << 1) & 0xFFFF, (recv_seq << 1) & 0xFFFF)
    return _build_apdu(ctrl + asdu)


def encode_s_frame(recv_seq: int) -> bytes:
    """编码仅确认用途的 S-frame。

    Args:
        recv_seq: N(R) 接收确认序号。

    Returns:
        固定 6 字节 APDU。

    Raises:
        ProtocolError: 序号越界。
    """
    _validate_seq(recv_seq, "recv_seq")

    # S-frame 4 字节控制域：
    #   bytes 0-1: 0x0001 (S-frame marker: bit 0 = 1, bit 1 = 0)
    #   bytes 2-3: recv_seq << 1
    ctrl = struct.pack("<H", 0x0001)  # bits 0-1 = 01
    ctrl += struct.pack("<H", (recv_seq << 1) & 0xFFFF)
    return _build_apdu(ctrl)


def encode_u_frame(frame_type: UFrameType) -> bytes:
    """编码 U-frame。

    Args:
        frame_type: UFrameType 功能码。

    Returns:
        固定 6 字节 APDU。
    """
    ctrl = bytes([frame_type.value, 0x00, 0x00, 0x00])
    return _build_apdu(ctrl)


def encode_apdu(frame: APDUFrame) -> bytes:
    """把 I/S/U frame 统一编码为 APDU。

    Args:
        frame: IFrame、SFrame 或 UFrame。

    Returns:
        完整 APDU 字节。

    Raises:
        ProtocolError: 传入未知 frame 类型。
    """
    if isinstance(frame, UFrame):
        return encode_u_frame(frame.frame_type)
    elif isinstance(frame, SFrame):
        return encode_s_frame(frame.recv_seq)
    elif isinstance(frame, IFrame):
        return encode_i_frame(frame.send_seq, frame.recv_seq, frame.asdu)
    else:
        raise ProtocolError(f"Unknown frame type: {type(frame).__name__}")
