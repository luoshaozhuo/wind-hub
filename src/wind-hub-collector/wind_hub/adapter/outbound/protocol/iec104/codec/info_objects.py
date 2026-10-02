"""IEC104 information object 数据模型与 TypeID codec。

每个 decode_* 函数从指定 offset 解析一个 information object，并返回
(object, new_offset)；encode_* 执行逆向编码。该模块只做字节转换，不执行网络
I/O，也不维护 session 状态。缓冲区不足或字段非法时统一抛 ProtocolError。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from wind_hub.adapter.outbound.protocol.iec104.codec.ioa import decode_ioa, encode_ioa
from wind_hub.adapter.outbound.protocol.iec104.codec.time import (
    CP56Time2a,
    decode_cp56time2a,
    encode_cp56time2a,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import QualityFlag
from wind_hub.domain.model.errors import ProtocolError

# ==========================================================================
# helpers
# ==========================================================================


def _decode_quality(data: bytes, off: int) -> tuple[QualityFlag, int]:
    """解码一字节 QDS。

    Args:
        data: 原始字节缓冲区。
        off: QDS 起始偏移。

    Returns:
        (QualityFlag, new_offset)。

    Raises:
        ProtocolError: 剩余字节不足。
    """
    if off >= len(data):
        raise ProtocolError(f"QDS decode: need 1 byte at offset {off}, " f"got {len(data) - off}")
    return QualityFlag(data[off]), off + 1


def _encode_quality(q: QualityFlag) -> bytes:
    """把 QualityFlag 编码为一字节 QDS。"""
    return bytes([q.value & 0x1F])  # only low 5 bits


def _check_len(data: bytes, offset: int, need: int, label: str) -> None:
    """检查缓冲区剩余长度。

    Raises:
        ProtocolError: 从 offset 起不足 need 字节。
    """
    if len(data) - offset < need:
        raise ProtocolError(
            f"{label} decode: need {need} bytes at offset {offset}, " f"got {len(data) - offset}"
        )


# ==========================================================================
# M_SP_NA_1：单点信息（TypeID=1）
# ==========================================================================


@dataclass(frozen=True)
class SinglePoint:
    """M_SP_NA_1 — 单点信息 (1 bit value)."""

    ioa: int
    value: bool  # True = ON, False = OFF
    quality: QualityFlag = QualityFlag(0)


def decode_m_sp_na_1(data: bytes, offset: int) -> tuple[SinglePoint, int]:
    """解码 M_SP_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (SinglePoint, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 4, "M_SP_NA_1")
    ioa, off = decode_ioa(data, offset)
    siq = data[off]
    quality = QualityFlag((siq >> 3) & 0x1E)
    value = bool(siq & 0x01)
    return SinglePoint(ioa=ioa, value=value, quality=quality), off + 1


def encode_m_sp_na_1(obj: SinglePoint) -> bytes:
    """编码 M_SP_NA_1 information object。

    Args:
        obj: 待编码的 SinglePoint。

    Returns:
        对应 information object 的 wire bytes。
    """
    siq = ((obj.quality.value & 0x1E) << 3) | (0x01 if obj.value else 0x00)
    return encode_ioa(obj.ioa) + bytes([siq])


# ==========================================================================
# M_DP_NA_1：双点信息（TypeID=3）
# ==========================================================================


@dataclass(frozen=True)
class DoublePoint:
    """M_DP_NA_1 — 双点信息 (2 bit value)."""

    ioa: int
    value: int  # 0=indeterminate, 1=OFF, 2=ON, 3=indeterminate
    quality: QualityFlag = QualityFlag(0)


def decode_m_dp_na_1(data: bytes, offset: int) -> tuple[DoublePoint, int]:
    """解码 M_DP_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (DoublePoint, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 4, "M_DP_NA_1")
    ioa, off = decode_ioa(data, offset)
    diq = data[off]
    quality = QualityFlag((diq >> 3) & 0x1E)
    value = diq & 0x03
    return DoublePoint(ioa=ioa, value=value, quality=quality), off + 1


def encode_m_dp_na_1(obj: DoublePoint) -> bytes:
    """编码 M_DP_NA_1 information object。

    Args:
        obj: 待编码的 DoublePoint。

    Returns:
        对应 information object 的 wire bytes。
    """
    diq = ((obj.quality.value & 0x1E) << 3) | (obj.value & 0x03)
    return encode_ioa(obj.ioa) + bytes([diq])


# ==========================================================================
# M_ME_NA_1：归一化测量值（TypeID=9）
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueNormalized:
    """M_ME_NA_1 — 归一化测量值 (-1.0 to 1.0-epsilon)."""

    ioa: int
    value: float  # [-1.0, ~1.0]
    quality: QualityFlag = QualityFlag(0)


_NORM_SCALE = float(0x7FFF)


def _norm_to_int16(value: float) -> int:
    """把归一化浮点值钳位并转换为 int16。"""
    clamped = max(-1.0, min(1.0, value))
    raw = int(clamped * _NORM_SCALE)
    return max(-32768, min(32767, raw))


def _norm_from_int16(raw: int) -> float:
    """把 int16 还原为归一化浮点值。"""
    if raw == -32768:
        return -1.0  # -32768 represents -1.0 exactly
    return raw / _NORM_SCALE


def decode_m_me_na_1(data: bytes, offset: int) -> tuple[MeasuredValueNormalized, int]:
    """解码 M_ME_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (MeasuredValueNormalized, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 6, "M_ME_NA_1")
    ioa, off = decode_ioa(data, offset)
    raw = struct.unpack_from("<h", data, off)[0]
    off += 2
    quality, off = _decode_quality(data, off)
    return MeasuredValueNormalized(
        ioa=ioa,
        value=_norm_from_int16(raw),
        quality=quality,
    ), off


def encode_m_me_na_1(obj: MeasuredValueNormalized) -> bytes:
    """编码 M_ME_NA_1 information object。

    Args:
        obj: 待编码的 MeasuredValueNormalized。

    Returns:
        对应 information object 的 wire bytes。
    """
    raw = _norm_to_int16(obj.value)
    return encode_ioa(obj.ioa) + struct.pack("<h", raw) + _encode_quality(obj.quality)


# ==========================================================================
# M_ME_NB_1：标度化测量值（TypeID=11）
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueScaled:
    """M_ME_NB_1 — 标度化测量值 (-32768 to 32767)."""

    ioa: int
    value: int  # range [-32768, 32767]
    quality: QualityFlag = QualityFlag(0)


def decode_m_me_nb_1(data: bytes, offset: int) -> tuple[MeasuredValueScaled, int]:
    """解码 M_ME_NB_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (MeasuredValueScaled, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 6, "M_ME_NB_1")
    ioa, off = decode_ioa(data, offset)
    raw = struct.unpack_from("<h", data, off)[0]
    off += 2
    quality, off = _decode_quality(data, off)
    return MeasuredValueScaled(ioa=ioa, value=raw, quality=quality), off


def encode_m_me_nb_1(obj: MeasuredValueScaled) -> bytes:
    """编码 M_ME_NB_1 information object。

    Args:
        obj: 待编码的 MeasuredValueScaled。

    Returns:
        对应 information object 的 wire bytes。
    """
    return encode_ioa(obj.ioa) + struct.pack("<h", obj.value) + _encode_quality(obj.quality)


# ==========================================================================
# M_ME_NC_1：短浮点测量值（TypeID=13）
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueShort:
    """M_ME_NC_1 — 短浮点测量值 (IEEE 754 single-precision)."""

    ioa: int
    value: float
    quality: QualityFlag = QualityFlag(0)


def decode_m_me_nc_1(data: bytes, offset: int) -> tuple[MeasuredValueShort, int]:
    """解码 M_ME_NC_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (MeasuredValueShort, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 8, "M_ME_NC_1")
    ioa, off = decode_ioa(data, offset)
    value = struct.unpack_from("<f", data, off)[0]
    off += 4
    quality, off = _decode_quality(data, off)
    return MeasuredValueShort(ioa=ioa, value=value, quality=quality), off


def encode_m_me_nc_1(obj: MeasuredValueShort) -> bytes:
    """编码 M_ME_NC_1 information object。

    Args:
        obj: 待编码的 MeasuredValueShort。

    Returns:
        对应 information object 的 wire bytes。
    """
    return encode_ioa(obj.ioa) + struct.pack("<f", obj.value) + _encode_quality(obj.quality)


# ==========================================================================
# M_SP_TB_1：带 CP56Time2a 的单点信息（TypeID=30）
# ==========================================================================


@dataclass(frozen=True)
class SinglePointWithTime:
    """M_SP_TB_1 — 单点信息 带时标."""

    ioa: int
    value: bool
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_sp_tb_1(data: bytes, offset: int) -> tuple[SinglePointWithTime, int]:
    """解码 M_SP_TB_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (SinglePointWithTime, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 11, "M_SP_TB_1")
    ioa, off = decode_ioa(data, offset)
    siq = data[off]
    quality = QualityFlag((siq >> 3) & 0x1E)
    value = bool(siq & 0x01)
    off += 1
    ts, off = decode_cp56time2a(data, off)
    return SinglePointWithTime(
        ioa=ioa,
        value=value,
        quality=quality,
        timestamp=ts,
    ), off


def encode_m_sp_tb_1(obj: SinglePointWithTime) -> bytes:
    """编码 M_SP_TB_1 information object。

    Args:
        obj: 待编码的 SinglePointWithTime。

    Returns:
        对应 information object 的 wire bytes。
    """
    siq = ((obj.quality.value & 0x1E) << 3) | (0x01 if obj.value else 0x00)
    ts = obj.timestamp or CP56Time2a(
        milliseconds=0,
        minutes=0,
        hours=0,
        day=1,
        month=1,
        year=2000,
    )
    return encode_ioa(obj.ioa) + bytes([siq]) + encode_cp56time2a(ts)


# ==========================================================================
# M_DP_TB_1：带 CP56Time2a 的双点信息（TypeID=31）
# ==========================================================================


@dataclass(frozen=True)
class DoublePointWithTime:
    """M_DP_TB_1 — 双点信息 带时标."""

    ioa: int
    value: int  # 0=indeterminate, 1=OFF, 2=ON, 3=indeterminate
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_dp_tb_1(data: bytes, offset: int) -> tuple[DoublePointWithTime, int]:
    """解码 M_DP_TB_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (DoublePointWithTime, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 11, "M_DP_TB_1")
    ioa, off = decode_ioa(data, offset)
    diq = data[off]
    quality = QualityFlag((diq >> 3) & 0x1E)
    value = diq & 0x03
    off += 1
    ts, off = decode_cp56time2a(data, off)
    return DoublePointWithTime(
        ioa=ioa,
        value=value,
        quality=quality,
        timestamp=ts,
    ), off


def encode_m_dp_tb_1(obj: DoublePointWithTime) -> bytes:
    """编码 M_DP_TB_1 information object。

    Args:
        obj: 待编码的 DoublePointWithTime。

    Returns:
        对应 information object 的 wire bytes。
    """
    diq = ((obj.quality.value & 0x1E) << 3) | (obj.value & 0x03)
    ts = obj.timestamp or CP56Time2a(
        milliseconds=0,
        minutes=0,
        hours=0,
        day=1,
        month=1,
        year=2000,
    )
    return encode_ioa(obj.ioa) + bytes([diq]) + encode_cp56time2a(ts)


# ==========================================================================
# M_ME_TD_1：带 CP56Time2a 的归一化测量值（TypeID=34）
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueNormalizedWithTime:
    """M_ME_TD_1 — 归一化测量值 带时标."""

    ioa: int
    value: float
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_me_td_1(data: bytes, offset: int) -> tuple[MeasuredValueNormalizedWithTime, int]:
    """解码 M_ME_TD_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (MeasuredValueNormalizedWithTime, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 13, "M_ME_TD_1")
    ioa, off = decode_ioa(data, offset)
    raw = struct.unpack_from("<h", data, off)[0]
    off += 2
    quality, off = _decode_quality(data, off)
    ts, off = decode_cp56time2a(data, off)
    return MeasuredValueNormalizedWithTime(
        ioa=ioa,
        value=_norm_from_int16(raw),
        quality=quality,
        timestamp=ts,
    ), off


def encode_m_me_td_1(obj: MeasuredValueNormalizedWithTime) -> bytes:
    """编码 M_ME_TD_1 information object。

    Args:
        obj: 待编码的 MeasuredValueNormalizedWithTime。

    Returns:
        对应 information object 的 wire bytes。
    """
    raw = _norm_to_int16(obj.value)
    ts = obj.timestamp or CP56Time2a(
        milliseconds=0,
        minutes=0,
        hours=0,
        day=1,
        month=1,
        year=2000,
    )
    return (
        encode_ioa(obj.ioa)
        + struct.pack("<h", raw)
        + _encode_quality(obj.quality)
        + encode_cp56time2a(ts)
    )


# ==========================================================================
# M_ME_TF_1：带 CP56Time2a 的短浮点测量值（TypeID=36）
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueShortWithTime:
    """M_ME_TF_1 — 短浮点测量值 带时标."""

    ioa: int
    value: float
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_me_tf_1(data: bytes, offset: int) -> tuple[MeasuredValueShortWithTime, int]:
    """解码 M_ME_TF_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (MeasuredValueShortWithTime, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 15, "M_ME_TF_1")
    ioa, off = decode_ioa(data, offset)
    value = struct.unpack_from("<f", data, off)[0]
    off += 4
    quality, off = _decode_quality(data, off)
    ts, off = decode_cp56time2a(data, off)
    return MeasuredValueShortWithTime(
        ioa=ioa,
        value=value,
        quality=quality,
        timestamp=ts,
    ), off


def encode_m_me_tf_1(obj: MeasuredValueShortWithTime) -> bytes:
    """编码 M_ME_TF_1 information object。

    Args:
        obj: 待编码的 MeasuredValueShortWithTime。

    Returns:
        对应 information object 的 wire bytes。
    """
    ts = obj.timestamp or CP56Time2a(
        milliseconds=0,
        minutes=0,
        hours=0,
        day=1,
        month=1,
        year=2000,
    )
    return (
        encode_ioa(obj.ioa)
        + struct.pack("<f", obj.value)
        + _encode_quality(obj.quality)
        + encode_cp56time2a(ts)
    )


# ==========================================================================
# C_SC_NA_1：单点遥控（TypeID=45）
# ==========================================================================


@dataclass(frozen=True)
class SingleCommand:
    """C_SC_NA_1 — 单点遥控."""

    ioa: int
    value: bool  # True = ON, False = OFF
    select: bool = False  # SCO bit 6: 0=execute, 1=select


def decode_c_sc_na_1(data: bytes, offset: int) -> tuple[SingleCommand, int]:
    """解码 C_SC_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (SingleCommand, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 4, "C_SC_NA_1")
    ioa, off = decode_ioa(data, offset)
    sco = data[off]
    value = bool(sco & 0x01)
    select = bool(sco & 0x80)  # bit 7 (S/E)
    return SingleCommand(ioa=ioa, value=value, select=select), off + 1


def encode_c_sc_na_1(obj: SingleCommand) -> bytes:
    """编码 C_SC_NA_1 information object。

    Args:
        obj: 待编码的 SingleCommand。

    Returns:
        对应 information object 的 wire bytes。
    """
    sco = 0x00
    if obj.value:
        sco |= 0x01
    if obj.select:
        sco |= 0x80
    return encode_ioa(obj.ioa) + bytes([sco])


# ==========================================================================
# C_DC_NA_1：双点遥控（TypeID=46）
# ==========================================================================


@dataclass(frozen=True)
class DoubleCommand:
    """C_DC_NA_1 — 双点遥控."""

    ioa: int
    value: int  # 1=OFF, 2=ON
    select: bool = False


def decode_c_dc_na_1(data: bytes, offset: int) -> tuple[DoubleCommand, int]:
    """解码 C_DC_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (DoubleCommand, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 4, "C_DC_NA_1")
    ioa, off = decode_ioa(data, offset)
    dco = data[off]
    value = dco & 0x03
    select = bool(dco & 0x80)
    return DoubleCommand(ioa=ioa, value=value, select=select), off + 1


def encode_c_dc_na_1(obj: DoubleCommand) -> bytes:
    """编码 C_DC_NA_1 information object。

    Args:
        obj: 待编码的 DoubleCommand。

    Returns:
        对应 information object 的 wire bytes。
    """
    dco = obj.value & 0x03
    if obj.select:
        dco |= 0x80
    return encode_ioa(obj.ioa) + bytes([dco])


# ==========================================================================
# C_SE_NC_1 — Set-point command, short floating-point (TypeID=50)
# ==========================================================================


@dataclass(frozen=True)
class SetpointCommandShort:
    """C_SE_NC_1 — 短浮点设点."""

    ioa: int
    value: float
    select: bool = False  # QOS bit 7


def decode_c_se_nc_1(data: bytes, offset: int) -> tuple[SetpointCommandShort, int]:
    """解码 C_SE_NC_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (SetpointCommandShort, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 8, "C_SE_NC_1")
    ioa, off = decode_ioa(data, offset)
    value = struct.unpack_from("<f", data, off)[0]
    off += 4
    qos = data[off]
    select = bool(qos & 0x80)
    return SetpointCommandShort(ioa=ioa, value=value, select=select), off + 1


def encode_c_se_nc_1(obj: SetpointCommandShort) -> bytes:
    """编码 C_SE_NC_1 information object。

    Args:
        obj: 待编码的 SetpointCommandShort。

    Returns:
        对应 information object 的 wire bytes。
    """
    qos = 0x80 if obj.select else 0x00
    return encode_ioa(obj.ioa) + struct.pack("<f", obj.value) + bytes([qos])


def decode_c_se_nc_1_response(
    data: bytes,
    offset: int,
) -> tuple[SetpointCommandShort, int]:
    """解码 C_SE_NC_1 激活确认/终止的短格式响应。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (SetpointCommandShort, new_offset)。确认帧不携带设点值，因此 value 仅为占位。

    Raises:
        ProtocolError: 缓冲区不足。
    """
    _check_len(data, offset, 4, "C_SE_NC_1 con")
    ioa, off = decode_ioa(data, offset)
    qos = data[off]
    select = bool(qos & 0x80)
    return SetpointCommandShort(ioa=ioa, value=0.0, select=select), off + 1


# ==========================================================================
# C_IC_NA_1：总召命令（TypeID=100）
# ==========================================================================


@dataclass(frozen=True)
class InterrogationCommand:
    """C_IC_NA_1 — 总召命令."""

    ioa: int = 0


def decode_c_ic_na_1(data: bytes, offset: int) -> tuple[InterrogationCommand, int]:
    """解码 C_IC_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (InterrogationCommand, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 4, "C_IC_NA_1")
    ioa, off = decode_ioa(data, offset)
    # QOI=20 表示站总召，21~36 表示组召；当前只需要跳过该字段。
    return InterrogationCommand(ioa=ioa), off + 1


def encode_c_ic_na_1(obj: InterrogationCommand) -> bytes:
    """编码 C_IC_NA_1 information object。

    Args:
        obj: 待编码的 InterrogationCommand。

    Returns:
        对应 information object 的 wire bytes。
    """
    return encode_ioa(obj.ioa) + b"\x14"  # QOI = 20 (station interrogation)


# ==========================================================================
# C_CI_NA_1：电能总召命令（TypeID=103）
# ==========================================================================


@dataclass(frozen=True)
class CounterInterrogationCommand:
    """C_CI_NA_1 — 电能总召命令."""

    ioa: int = 0


def decode_c_ci_na_1(data: bytes, offset: int) -> tuple[CounterInterrogationCommand, int]:
    """解码 C_CI_NA_1 information object。

    Args:
        data: 原始字节缓冲区。
        offset: information object 起始偏移。

    Returns:
        (CounterInterrogationCommand, new_offset)。

    Raises:
        ProtocolError: 缓冲区不足或字段编码非法。
    """
    _check_len(data, offset, 4, "C_CI_NA_1")
    ioa, off = decode_ioa(data, offset)
    return CounterInterrogationCommand(ioa=ioa), off + 1


def encode_c_ci_na_1(obj: CounterInterrogationCommand) -> bytes:
    """编码 C_CI_NA_1 information object。

    Args:
        obj: 待编码的 CounterInterrogationCommand。

    Returns:
        对应 information object 的 wire bytes。
    """
    return encode_ioa(obj.ioa) + b"\x05"  # QCC=5：general counter request。
