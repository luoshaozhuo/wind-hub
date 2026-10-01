"""IEC 60870-5-104 information object codecs — one encode/decode pair per TypeID."""

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
    """Decode 1-byte QDS field.

    Returns:
        ``(QualityFlag, new_offset)``.
    """
    if off >= len(data):
        raise ProtocolError(f"QDS decode: need 1 byte at offset {off}, " f"got {len(data) - off}")
    return QualityFlag(data[off]), off + 1


def _encode_quality(q: QualityFlag) -> bytes:
    """Encode QDS as 1 byte."""
    return bytes([q.value & 0x1F])  # only low 5 bits


def _check_len(data: bytes, offset: int, need: int, label: str) -> None:
    """Raise ProtocolError if not enough bytes remain."""
    if len(data) - offset < need:
        raise ProtocolError(
            f"{label} decode: need {need} bytes at offset {offset}, " f"got {len(data) - offset}"
        )


# ==========================================================================
# M_SP_NA_1 — Single-point information (TypeID=1)
# ==========================================================================


@dataclass(frozen=True)
class SinglePoint:
    """M_SP_NA_1 — 单点信息 (1 bit value)."""

    ioa: int
    value: bool  # True = ON, False = OFF
    quality: QualityFlag = QualityFlag(0)


def decode_m_sp_na_1(data: bytes, offset: int) -> tuple[SinglePoint, int]:
    """Decode M_SP_NA_1: IOA(3) + SIQ(1) = 4 bytes."""
    _check_len(data, offset, 4, "M_SP_NA_1")
    ioa, off = decode_ioa(data, offset)
    siq = data[off]
    quality = QualityFlag((siq >> 3) & 0x1E)
    value = bool(siq & 0x01)
    return SinglePoint(ioa=ioa, value=value, quality=quality), off + 1


def encode_m_sp_na_1(obj: SinglePoint) -> bytes:
    """Encode M_SP_NA_1."""
    siq = ((obj.quality.value & 0x1E) << 3) | (0x01 if obj.value else 0x00)
    return encode_ioa(obj.ioa) + bytes([siq])


# ==========================================================================
# M_DP_NA_1 — Double-point information (TypeID=3)
# ==========================================================================


@dataclass(frozen=True)
class DoublePoint:
    """M_DP_NA_1 — 双点信息 (2 bit value)."""

    ioa: int
    value: int  # 0=indeterminate, 1=OFF, 2=ON, 3=indeterminate
    quality: QualityFlag = QualityFlag(0)


def decode_m_dp_na_1(data: bytes, offset: int) -> tuple[DoublePoint, int]:
    """Decode M_DP_NA_1: IOA(3) + DIQ(1) = 4 bytes."""
    _check_len(data, offset, 4, "M_DP_NA_1")
    ioa, off = decode_ioa(data, offset)
    diq = data[off]
    quality = QualityFlag((diq >> 3) & 0x1E)
    value = diq & 0x03
    return DoublePoint(ioa=ioa, value=value, quality=quality), off + 1


def encode_m_dp_na_1(obj: DoublePoint) -> bytes:
    """Encode M_DP_NA_1."""
    diq = ((obj.quality.value & 0x1E) << 3) | (obj.value & 0x03)
    return encode_ioa(obj.ioa) + bytes([diq])


# ==========================================================================
# M_ME_NA_1 — Normalized measured value (TypeID=9)
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueNormalized:
    """M_ME_NA_1 — 归一化测量值 (-1.0 to 1.0-epsilon)."""

    ioa: int
    value: float  # [-1.0, ~1.0]
    quality: QualityFlag = QualityFlag(0)


_NORM_SCALE = float(0x7FFF)


def _norm_to_int16(value: float) -> int:
    """Convert normalized float to int16."""
    clamped = max(-1.0, min(1.0, value))
    raw = int(clamped * _NORM_SCALE)
    return max(-32768, min(32767, raw))


def _norm_from_int16(raw: int) -> float:
    """Convert int16 to normalized float."""
    if raw == -32768:
        return -1.0  # -32768 represents -1.0 exactly
    return raw / _NORM_SCALE


def decode_m_me_na_1(data: bytes, offset: int) -> tuple[MeasuredValueNormalized, int]:
    """Decode M_ME_NA_1: IOA(3) + 归一化值(2) + QDS(1) = 6 bytes."""
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
    """Encode M_ME_NA_1."""
    raw = _norm_to_int16(obj.value)
    return encode_ioa(obj.ioa) + struct.pack("<h", raw) + _encode_quality(obj.quality)


# ==========================================================================
# M_ME_NB_1 — Scaled measured value (TypeID=11)
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueScaled:
    """M_ME_NB_1 — 标度化测量值 (-32768 to 32767)."""

    ioa: int
    value: int  # range [-32768, 32767]
    quality: QualityFlag = QualityFlag(0)


def decode_m_me_nb_1(data: bytes, offset: int) -> tuple[MeasuredValueScaled, int]:
    """Decode M_ME_NB_1: IOA(3) + 标度化值(2) + QDS(1) = 6 bytes."""
    _check_len(data, offset, 6, "M_ME_NB_1")
    ioa, off = decode_ioa(data, offset)
    raw = struct.unpack_from("<h", data, off)[0]
    off += 2
    quality, off = _decode_quality(data, off)
    return MeasuredValueScaled(ioa=ioa, value=raw, quality=quality), off


def encode_m_me_nb_1(obj: MeasuredValueScaled) -> bytes:
    """Encode M_ME_NB_1."""
    return encode_ioa(obj.ioa) + struct.pack("<h", obj.value) + _encode_quality(obj.quality)


# ==========================================================================
# M_ME_NC_1 — Short floating-point measured value (TypeID=13)
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueShort:
    """M_ME_NC_1 — 短浮点测量值 (IEEE 754 single-precision)."""

    ioa: int
    value: float
    quality: QualityFlag = QualityFlag(0)


def decode_m_me_nc_1(data: bytes, offset: int) -> tuple[MeasuredValueShort, int]:
    """Decode M_ME_NC_1: IOA(3) + 短浮点(4) + QDS(1) = 8 bytes."""
    _check_len(data, offset, 8, "M_ME_NC_1")
    ioa, off = decode_ioa(data, offset)
    value = struct.unpack_from("<f", data, off)[0]
    off += 4
    quality, off = _decode_quality(data, off)
    return MeasuredValueShort(ioa=ioa, value=value, quality=quality), off


def encode_m_me_nc_1(obj: MeasuredValueShort) -> bytes:
    """Encode M_ME_NC_1."""
    return encode_ioa(obj.ioa) + struct.pack("<f", obj.value) + _encode_quality(obj.quality)


# ==========================================================================
# M_SP_TB_1 — Single-point with CP56Time2a (TypeID=30)
# ==========================================================================


@dataclass(frozen=True)
class SinglePointWithTime:
    """M_SP_TB_1 — 单点信息 带时标."""

    ioa: int
    value: bool
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_sp_tb_1(data: bytes, offset: int) -> tuple[SinglePointWithTime, int]:
    """Decode M_SP_TB_1: IOA(3) + SIQ(1) + CP56Time2a(7) = 11 bytes."""
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
    """Encode M_SP_TB_1."""
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
# M_DP_TB_1 — Double-point with CP56Time2a (TypeID=31)
# ==========================================================================


@dataclass(frozen=True)
class DoublePointWithTime:
    """M_DP_TB_1 — 双点信息 带时标."""

    ioa: int
    value: int  # 0=indeterminate, 1=OFF, 2=ON, 3=indeterminate
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_dp_tb_1(data: bytes, offset: int) -> tuple[DoublePointWithTime, int]:
    """Decode M_DP_TB_1: IOA(3) + DIQ(1) + CP56Time2a(7) = 11 bytes."""
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
    """Encode M_DP_TB_1."""
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
# M_ME_TD_1 — Normalized measured value with CP56Time2a (TypeID=34)
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueNormalizedWithTime:
    """M_ME_TD_1 — 归一化测量值 带时标."""

    ioa: int
    value: float
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_me_td_1(data: bytes, offset: int) -> tuple[MeasuredValueNormalizedWithTime, int]:
    """Decode M_ME_TD_1: IOA(3) + 值(2) + QDS(1) + CP56Time2a(7) = 13 bytes."""
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
    """Encode M_ME_TD_1."""
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
# M_ME_TF_1 — Short floating-point measured value with CP56Time2a (TypeID=36)
# ==========================================================================


@dataclass(frozen=True)
class MeasuredValueShortWithTime:
    """M_ME_TF_1 — 短浮点测量值 带时标."""

    ioa: int
    value: float
    quality: QualityFlag = QualityFlag(0)
    timestamp: CP56Time2a | None = None


def decode_m_me_tf_1(data: bytes, offset: int) -> tuple[MeasuredValueShortWithTime, int]:
    """Decode M_ME_TF_1: IOA(3) + 值(4) + QDS(1) + CP56Time2a(7) = 15 bytes."""
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
    """Encode M_ME_TF_1."""
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
# C_SC_NA_1 — Single command (TypeID=45)
# ==========================================================================


@dataclass(frozen=True)
class SingleCommand:
    """C_SC_NA_1 — 单点遥控."""

    ioa: int
    value: bool  # True = ON, False = OFF
    select: bool = False  # SCO bit 6: 0=execute, 1=select


def decode_c_sc_na_1(data: bytes, offset: int) -> tuple[SingleCommand, int]:
    """Decode C_SC_NA_1: IOA(3) + SCO(1) = 4 bytes."""
    _check_len(data, offset, 4, "C_SC_NA_1")
    ioa, off = decode_ioa(data, offset)
    sco = data[off]
    value = bool(sco & 0x01)
    select = bool(sco & 0x80)  # bit 7 (S/E)
    return SingleCommand(ioa=ioa, value=value, select=select), off + 1


def encode_c_sc_na_1(obj: SingleCommand) -> bytes:
    """Encode C_SC_NA_1."""
    sco = 0x00
    if obj.value:
        sco |= 0x01
    if obj.select:
        sco |= 0x80
    return encode_ioa(obj.ioa) + bytes([sco])


# ==========================================================================
# C_DC_NA_1 — Double command (TypeID=46)
# ==========================================================================


@dataclass(frozen=True)
class DoubleCommand:
    """C_DC_NA_1 — 双点遥控."""

    ioa: int
    value: int  # 1=OFF, 2=ON
    select: bool = False


def decode_c_dc_na_1(data: bytes, offset: int) -> tuple[DoubleCommand, int]:
    """Decode C_DC_NA_1: IOA(3) + DCO(1) = 4 bytes."""
    _check_len(data, offset, 4, "C_DC_NA_1")
    ioa, off = decode_ioa(data, offset)
    dco = data[off]
    value = dco & 0x03
    select = bool(dco & 0x80)
    return DoubleCommand(ioa=ioa, value=value, select=select), off + 1


def encode_c_dc_na_1(obj: DoubleCommand) -> bytes:
    """Encode C_DC_NA_1."""
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
    """Decode C_SE_NC_1: IOA(3) + 值(4) + QOS(1) = 8 bytes."""
    _check_len(data, offset, 8, "C_SE_NC_1")
    ioa, off = decode_ioa(data, offset)
    value = struct.unpack_from("<f", data, off)[0]
    off += 4
    qos = data[off]
    select = bool(qos & 0x80)
    return SetpointCommandShort(ioa=ioa, value=value, select=select), off + 1


def encode_c_se_nc_1(obj: SetpointCommandShort) -> bytes:
    """Encode C_SE_NC_1."""
    qos = 0x80 if obj.select else 0x00
    return encode_ioa(obj.ioa) + struct.pack("<f", obj.value) + bytes([qos])


def decode_c_se_nc_1_response(
    data: bytes,
    offset: int,
) -> tuple[SetpointCommandShort, int]:
    """Decode a C_SE_NC_1 activation confirmation: IOA(3) + QOS(1) = 4 bytes.

    Unlike the activation command (8 bytes), the confirmation/termination
    echoes only the IOA and QOS (qualifier of setpoint command) — the value
    is omitted.  The command decoder ``decode_c_se_nc_1`` cannot be used here
    because it requires the 4-byte value field.  ``value`` is reported as
    ``0.0`` since the confirmation carries none.
    """
    _check_len(data, offset, 4, "C_SE_NC_1 con")
    ioa, off = decode_ioa(data, offset)
    qos = data[off]
    select = bool(qos & 0x80)
    return SetpointCommandShort(ioa=ioa, value=0.0, select=select), off + 1


# ==========================================================================
# C_IC_NA_1 — Interrogation command (TypeID=100)
# ==========================================================================


@dataclass(frozen=True)
class InterrogationCommand:
    """C_IC_NA_1 — 总召命令."""

    ioa: int = 0


def decode_c_ic_na_1(data: bytes, offset: int) -> tuple[InterrogationCommand, int]:
    """Decode C_IC_NA_1: IOA(3) + QOI(1) = 4 bytes."""
    _check_len(data, offset, 4, "C_IC_NA_1")
    ioa, off = decode_ioa(data, offset)
    # QOI: 20=station, 21-36=group 1-16; we just skip it
    return InterrogationCommand(ioa=ioa), off + 1


def encode_c_ic_na_1(obj: InterrogationCommand) -> bytes:
    """Encode C_IC_NA_1."""
    return encode_ioa(obj.ioa) + b"\x14"  # QOI = 20 (station interrogation)


# ==========================================================================
# C_CI_NA_1 — Counter interrogation command (TypeID=103)
# ==========================================================================


@dataclass(frozen=True)
class CounterInterrogationCommand:
    """C_CI_NA_1 — 电能总召命令."""

    ioa: int = 0


def decode_c_ci_na_1(data: bytes, offset: int) -> tuple[CounterInterrogationCommand, int]:
    """Decode C_CI_NA_1: IOA(3) + QCC(1) = 4 bytes."""
    _check_len(data, offset, 4, "C_CI_NA_1")
    ioa, off = decode_ioa(data, offset)
    return CounterInterrogationCommand(ioa=ioa), off + 1


def encode_c_ci_na_1(obj: CounterInterrogationCommand) -> bytes:
    """Encode C_CI_NA_1."""
    return encode_ioa(obj.ioa) + b"\x05"  # QCC = 5 (general counter request)
