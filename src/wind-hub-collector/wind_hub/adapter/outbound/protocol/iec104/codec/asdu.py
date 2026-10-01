"""IEC 60870-5-104 ASDU (Application Service Data Unit) codec."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    decode_c_ci_na_1,
    decode_c_dc_na_1,
    decode_c_ic_na_1,
    decode_c_sc_na_1,
    decode_c_se_nc_1,
    decode_c_se_nc_1_response,
    decode_m_dp_na_1,
    decode_m_dp_tb_1,
    decode_m_me_na_1,
    decode_m_me_nb_1,
    decode_m_me_nc_1,
    decode_m_me_td_1,
    decode_m_me_tf_1,
    decode_m_sp_na_1,
    decode_m_sp_tb_1,
    encode_c_ci_na_1,
    encode_c_dc_na_1,
    encode_c_ic_na_1,
    encode_c_sc_na_1,
    encode_c_se_nc_1,
    encode_m_dp_na_1,
    encode_m_dp_tb_1,
    encode_m_me_na_1,
    encode_m_me_nb_1,
    encode_m_me_nc_1,
    encode_m_me_td_1,
    encode_m_me_tf_1,
    encode_m_sp_na_1,
    encode_m_sp_tb_1,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    TypeID,
)
from wind_hub.domain.model.errors import ProtocolError

# ==========================================================================
# type-dispatch tables
# ==========================================================================

_CODEC_REGISTRY: dict[TypeID, tuple[Callable[..., Any], Callable[..., bytes]]] = {
    # monitor direction — without time tag
    TypeID.M_SP_NA_1: (decode_m_sp_na_1, encode_m_sp_na_1),
    TypeID.M_DP_NA_1: (decode_m_dp_na_1, encode_m_dp_na_1),
    TypeID.M_ME_NA_1: (decode_m_me_na_1, encode_m_me_na_1),
    TypeID.M_ME_NB_1: (decode_m_me_nb_1, encode_m_me_nb_1),
    TypeID.M_ME_NC_1: (decode_m_me_nc_1, encode_m_me_nc_1),
    # monitor direction — with CP56Time2a
    TypeID.M_SP_TB_1: (decode_m_sp_tb_1, encode_m_sp_tb_1),
    TypeID.M_DP_TB_1: (decode_m_dp_tb_1, encode_m_dp_tb_1),
    TypeID.M_ME_TD_1: (decode_m_me_td_1, encode_m_me_td_1),
    TypeID.M_ME_TF_1: (decode_m_me_tf_1, encode_m_me_tf_1),
    # control direction
    TypeID.C_SC_NA_1: (decode_c_sc_na_1, encode_c_sc_na_1),
    TypeID.C_DC_NA_1: (decode_c_dc_na_1, encode_c_dc_na_1),
    TypeID.C_SE_NC_1: (decode_c_se_nc_1, encode_c_se_nc_1),
    TypeID.C_IC_NA_1: (decode_c_ic_na_1, encode_c_ic_na_1),
    TypeID.C_CI_NA_1: (decode_c_ci_na_1, encode_c_ci_na_1),
}

# Dict mapping TypeID to info-object size for SQ=1 (continuous address) mode.
# When SQ=1, each info object after the first shares the same size.
_INFO_OBJECT_SIZES: dict[TypeID, int] = {
    TypeID.M_SP_NA_1: 4,
    TypeID.M_SP_TB_1: 11,
    TypeID.M_DP_NA_1: 4,
    TypeID.M_DP_TB_1: 11,
    TypeID.M_ME_NA_1: 6,
    TypeID.M_ME_NB_1: 6,
    TypeID.M_ME_NC_1: 8,
    TypeID.M_ME_TD_1: 13,
    TypeID.M_ME_TF_1: 15,
    TypeID.C_SC_NA_1: 4,
    TypeID.C_DC_NA_1: 4,
    TypeID.C_SE_NC_1: 8,
    TypeID.C_IC_NA_1: 4,
    TypeID.C_CI_NA_1: 4,
}


# ==========================================================================
# ASDU model
# ==========================================================================


@dataclass(frozen=True)
class ASDU:
    """IEC 60870-5-104 Application Service Data Unit.

    Byte layout (after APCI header)::

        byte 0:    TypeID
        byte 1:    VSQ (SQ bit 7, count bits 0–6)
        byte 2:    COT
        byte 3:    OA (originator address, usually 0)
        byte 4–5:  CA (common address, little-endian)
        byte 6+:   information objects
    """

    type_id: TypeID
    """ASDU type identifier."""

    cause: CauseOfTransmission
    """Cause of transmission."""

    common_address: int
    """Common address / station address (0–65535)."""

    objects: list[Any] = field(default_factory=list)
    """Information objects.  Type depends on ``type_id``."""

    is_sequence: bool = False
    """SQ bit — ``True`` means continuous (sequential) addressing."""

    originator_address: int = 0
    """Originator address (OA, usually 0)."""

    @property
    def count(self) -> int:
        """Number of information objects."""
        return len(self.objects)


# ==========================================================================
# decode
# ==========================================================================


def decode_asdu(data: bytes, offset: int = 0) -> tuple[ASDU, int]:
    """Decode an ASDU from *data* at *offset*.

    Returns:
        ``(ASDU, new_offset)`` tuple.

    Raises:
        ProtocolError: On unknown TypeID, insufficient data, or decode error.
    """
    if len(data) - offset < 6:
        raise ProtocolError(
            f"ASDU decode: need at least 6 header bytes at offset {offset}, "
            f"got {len(data) - offset}"
        )

    type_id_raw = data[offset]
    vsq = data[offset + 1]
    cot_raw = data[offset + 2]
    oa = data[offset + 3]
    ca = struct.unpack_from("<H", data, offset + 4)[0]
    off = offset + 6

    # Parse TypeID
    try:
        type_id = TypeID(type_id_raw)
    except ValueError:
        raise ProtocolError(f"Unknown TypeID {type_id_raw:#04x} at offset {offset}") from None

    # Parse VSQ
    count = vsq & 0x7F
    is_sequence = bool(vsq & 0x80)

    # Parse COT
    try:
        cause = CauseOfTransmission(cot_raw & 0x3F)
    except ValueError:
        raise ProtocolError(f"Unknown COT {cot_raw & 0x3F:#04x} at offset {offset + 2}") from None

    # Look up codec
    codec_pair = _CODEC_REGISTRY.get(type_id)
    if codec_pair is None:
        raise ProtocolError(f"No codec registered for TypeID {type_id.name} ({type_id_raw})")
    # C_SE_NC_1 activation confirmations/terminations (COT=7/10) echo only
    # IOA + QOS (4 bytes); the activation command (COT=6) carries the value
    # (8 bytes).  The default decoder assumes the command layout, so select
    # a dedicated confirmation decoder when the COT indicates a response.
    if type_id == TypeID.C_SE_NC_1 and cause in (
        CauseOfTransmission.ACTIVATION_CON,
        CauseOfTransmission.ACTIVATION_TERMINATION,
    ):
        decoder = cast(
            Callable[[bytes, int], tuple[Any, int]],
            decode_c_se_nc_1_response,
        )
    else:
        decoder = cast(Callable[[bytes, int], tuple[Any, int]], codec_pair[0])

    # Decode information objects
    objects: list[Any] = []
    if is_sequence and count > 0:
        elem_size = _INFO_OBJECT_SIZES.get(type_id)
        if elem_size is None:
            raise ProtocolError(f"No element size defined for TypeID {type_id.name} (SQ=1 mode)")
        for _i in range(count):
            if len(data) - off < elem_size:
                raise ProtocolError(
                    f"ASDU decode: need {elem_size} bytes for info object "
                    f"#{_i + 1} at offset {off}, got {len(data) - off}"
                )
            obj, off = decoder(data, off)
            objects.append(obj)
    else:
        for _i in range(count):
            obj, off = decoder(data, off)
            objects.append(obj)

    return ASDU(
        type_id=type_id,
        cause=cause,
        common_address=ca,
        objects=objects,
        is_sequence=is_sequence,
        originator_address=oa,
    ), off


# ==========================================================================
# encode
# ==========================================================================


def encode_asdu(asdu: ASDU) -> bytes:
    """Encode an ASDU to bytes.

    Raises:
        ProtocolError: On unknown TypeID or missing codec.
    """
    codec_pair = _CODEC_REGISTRY.get(asdu.type_id)
    if codec_pair is None:
        raise ProtocolError(f"No codec registered for TypeID {asdu.type_id.name}")

    encoder = cast(Callable[[Any], bytes], codec_pair[1])

    # Build header
    header = bytearray()
    header.append(asdu.type_id.value)  # TypeID

    vsq = len(asdu.objects) & 0x7F  # count, low 7 bits
    if asdu.is_sequence:
        vsq |= 0x80  # SQ bit
    header.append(vsq)

    header.append(asdu.cause.value & 0x3F)  # COT
    header.append(asdu.originator_address & 0xFF)  # OA
    header.extend(struct.pack("<H", asdu.common_address))  # CA

    # Encode information objects
    body = bytearray()
    for obj in asdu.objects:
        body.extend(encoder(obj))

    return bytes(header) + bytes(body)
