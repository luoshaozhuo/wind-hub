"""IEC 60870-5-104 ASDU 编码与解码。

本模块通过 TypeID dispatch table 选择 information object codec。objects 使用
list[Any] 是因为 ASDU 在 wire 层可承载多种强类型 information object；Any 只
存在于该协议联合边界，进入具体 codec 后会收敛为对应 dataclass。
"""

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
from wind_hub_core.model.errors import ProtocolError

# ==========================================================================
# TypeID codec 分发表
# ==========================================================================

_CODEC_REGISTRY: dict[TypeID, tuple[Callable[..., Any], Callable[..., bytes]]] = {
    # 监视方向：无时标
    TypeID.M_SP_NA_1: (decode_m_sp_na_1, encode_m_sp_na_1),
    TypeID.M_DP_NA_1: (decode_m_dp_na_1, encode_m_dp_na_1),
    TypeID.M_ME_NA_1: (decode_m_me_na_1, encode_m_me_na_1),
    TypeID.M_ME_NB_1: (decode_m_me_nb_1, encode_m_me_nb_1),
    TypeID.M_ME_NC_1: (decode_m_me_nc_1, encode_m_me_nc_1),
    # 监视方向：带 CP56Time2a
    TypeID.M_SP_TB_1: (decode_m_sp_tb_1, encode_m_sp_tb_1),
    TypeID.M_DP_TB_1: (decode_m_dp_tb_1, encode_m_dp_tb_1),
    TypeID.M_ME_TD_1: (decode_m_me_td_1, encode_m_me_td_1),
    TypeID.M_ME_TF_1: (decode_m_me_tf_1, encode_m_me_tf_1),
    # 控制方向
    TypeID.C_SC_NA_1: (decode_c_sc_na_1, encode_c_sc_na_1),
    TypeID.C_DC_NA_1: (decode_c_dc_na_1, encode_c_dc_na_1),
    TypeID.C_SE_NC_1: (decode_c_se_nc_1, encode_c_se_nc_1),
    TypeID.C_IC_NA_1: (decode_c_ic_na_1, encode_c_ic_na_1),
    TypeID.C_CI_NA_1: (decode_c_ci_na_1, encode_c_ci_na_1),
}

# SQ=1 连续地址模式下，各 TypeID 的单个 information object 固定长度。
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
# ASDU 数据模型
# ==========================================================================


@dataclass(frozen=True)
class ASDU:
    """IEC104 Application Service Data Unit。

    Attributes:
        type_id: ASDU TypeID。
        cause: 传送原因 COT。
        common_address: 公共地址/站地址。
        objects: information object 列表；具体类型由 type_id 决定。
        is_sequence: VSQ 的 SQ 位，True 表示连续地址模式。
        originator_address: OA，通常为 0。
    """

    type_id: TypeID
    """ASDU TypeID。"""

    cause: CauseOfTransmission
    """传送原因 COT。"""

    common_address: int
    """公共地址/站地址，范围 0~65535。"""

    objects: list[Any] = field(default_factory=list)
    """information object 列表；元素类型由 type_id 决定。"""

    is_sequence: bool = False
    """VSQ 的 SQ 位；True 表示连续地址模式。"""

    originator_address: int = 0
    """Originator Address，通常为 0。"""

    @property
    def count(self) -> int:
        """返回 information object 数量。"""
        return len(self.objects)


# ==========================================================================
# 解码
# ==========================================================================


def decode_asdu(data: bytes, offset: int = 0) -> tuple[ASDU, int]:
    """从指定 offset 解码一个 ASDU。

    Args:
        data: 包含 ASDU 的字节缓冲区。
        offset: ASDU 起始偏移。

    Returns:
        (ASDU, new_offset)。

    Raises:
        ProtocolError: Header 不完整、TypeID/COT 未知、codec 缺失或 object 数据不足。
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

    # 解析 TypeID。
    try:
        type_id = TypeID(type_id_raw)
    except ValueError:
        raise ProtocolError(f"Unknown TypeID {type_id_raw:#04x} at offset {offset}") from None

    # 解析 VSQ。
    count = vsq & 0x7F
    is_sequence = bool(vsq & 0x80)

    # 解析 COT。
    try:
        cause = CauseOfTransmission(cot_raw & 0x3F)
    except ValueError:
        raise ProtocolError(f"Unknown COT {cot_raw & 0x3F:#04x} at offset {offset + 2}") from None

    # 根据 TypeID 选择 codec。
    codec_pair = _CODEC_REGISTRY.get(type_id)
    if codec_pair is None:
        raise ProtocolError(f"No codec registered for TypeID {type_id.name} ({type_id_raw})")
    # C_SE_NC_1 的激活确认/终止只回显 IOA+QOS，不携带设点值；
    # 响应 COT 必须使用专用短格式 decoder。
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

    # 解码 information object。
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
# 编码
# ==========================================================================


def encode_asdu(asdu: ASDU) -> bytes:
    """把 ASDU 编码为 wire bytes。

    Args:
        asdu: 待编码 ASDU。

    Returns:
        不含 APCI 头的 ASDU 字节。

    Raises:
        ProtocolError: TypeID 未注册 codec。
    """
    codec_pair = _CODEC_REGISTRY.get(asdu.type_id)
    if codec_pair is None:
        raise ProtocolError(f"No codec registered for TypeID {asdu.type_id.name}")

    encoder = cast(Callable[[Any], bytes], codec_pair[1])

    # 组装 ASDU header。
    header = bytearray()
    header.append(asdu.type_id.value)  # TypeID

    vsq = len(asdu.objects) & 0x7F  # count, low 7 bits
    if asdu.is_sequence:
        vsq |= 0x80  # SQ bit
    header.append(vsq)

    header.append(asdu.cause.value & 0x3F)  # COT
    header.append(asdu.originator_address & 0xFF)  # OA
    header.extend(struct.pack("<H", asdu.common_address))  # CA

    # 编码 information object。
    body = bytearray()
    for obj in asdu.objects:
        body.extend(encoder(obj))

    return bytes(header) + bytes(body)
