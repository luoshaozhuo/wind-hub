"""IEC 60870-5-104 设备协议适配器及其公开 codec 类型。"""

# 本包公开 re-export codec 符号；F401 用于显式导出，若改用仅 __all__ 的延迟加载可移除。
from wind_hub_core.protocol.iec104.codec import (  # noqa: F401
    ASDU,
    APDUFrame,
    CauseOfTransmission,
    CP56Time2a,
    IFrame,
    QualityFlag,
    SFrame,
    TypeID,
    UFrame,
    UFrameType,
    decode_apdu,
    decode_asdu,
    decode_cp56time2a,
    decode_ioa,
    encode_apdu,
    encode_asdu,
    encode_cp56time2a,
    encode_ioa,
    from_datetime,
    to_datetime,
)

# 同上：Driver 作为包级公开 API 重导出。
from wind_hub_core.protocol.iec104.driver import IEC104Driver  # noqa: F401

__all__ = [
    "IEC104Driver",
    "TypeID",
    "CauseOfTransmission",
    "UFrameType",
    "QualityFlag",
    "CP56Time2a",
    "ASDU",
    "IFrame",
    "SFrame",
    "UFrame",
    "APDUFrame",
]
