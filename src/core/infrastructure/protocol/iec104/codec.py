"""hat.drivers.iec104 报文与 wind-hub 协议契约之间的转换。

- 监测方向：hat 的 ``DataMsg`` → ``ProtocolSample``（值、质量、CP56Time2a）；
- 控制方向：按点表 type_id 把写值构造为 hat 命令对象，严格对应协议类型，
  本地类型/范围校验失败抛 TypeError/ValueError，由 Driver 归为确定失败。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from hat.drivers.iec101 import common as iec101

from core.application.errors import ConfigError
from core.application.protocol_contract import (
    ProtocolSample,
    Quality,
    TimestampSource,
)
from core.domain.sample_value import PointScalar

from .mapping import IEC104Point


def decode_sample(
    point_id: str, msg: iec101.DataMsg, *, receive_time: datetime | None = None
) -> ProtocolSample:
    """把一条监测 ASDU（一个 IO 元素）解码为 ProtocolSample。

    合法数据携带 Invalid 等品质位时正常返回 quality=BAD 的真实样本，
    不在这里抛错；只有协议数据本身畸形/类型不受支持才抛异常。

    Raises:
        ValueError: 报文携带当前不支持的数据类型。主动读响应由调用方
            以 ProtocolError 显式完成请求 future；自发数据由调用方记
            错误日志并丢弃，两种情形都不伪造样本、不中断接收管线。
    """
    if receive_time is None:
        receive_time = datetime.now(UTC)
    timestamp, source = decode_timestamp(msg)
    value, quality = decode_data(cast(iec101.Data, msg.data))
    return ProtocolSample(
        point_id=point_id,
        value=value,
        timestamp=timestamp,
        quality=quality,
        timestamp_source=source,
        receive_time=receive_time,
        device_time=timestamp if source == "device" else None,
    )


def decode_timestamp(msg: iec101.DataMsg) -> tuple[datetime, TimestampSource]:
    """CP56Time2a → UTC datetime；无时标或时标无效时本地接收时刻兜底。"""
    time = getattr(msg, "time", None)
    if time is not None and not getattr(time, "invalid", False):
        # CP56Time2a 按设备本地时间编码（不含时区），hat 依主机本地时区
        # 归一到 UTC，是协议可提供的最佳设备时间证据。
        return iec101.time_to_datetime(time), "device"
    return datetime.now(UTC), "local"


def decode_data(data: iec101.Data) -> tuple[PointScalar, Quality]:
    """把 hat 数据 IO 元素转换为 (标量值, 统一质量)。"""
    quality = decode_quality(getattr(data, "quality", None))

    if isinstance(data, iec101.SingleData):
        return data.value is iec101.SingleValue.ON, quality
    if isinstance(data, iec101.DoubleData):
        return int(data.value.value), quality
    if isinstance(data, iec101.StepPositionData):
        # transient 表示设备处于调节过渡态，值仍可用但质量不确定。
        if quality is Quality.GOOD and data.value.transient:
            quality = Quality.UNCERTAIN
        return int(data.value.value), quality
    if isinstance(data, iec101.BitstringData):
        return int(data.value.value), quality
    if isinstance(data, iec101.NormalizedData):
        return float(data.value.value), quality
    if isinstance(data, iec101.ScaledData):
        return int(data.value.value), quality
    if isinstance(data, iec101.FloatingData):
        return float(data.value.value), quality
    if isinstance(data, iec101.BinaryCounterData):
        return int(data.value.value), quality
    if isinstance(data, iec101.StatusData):
        # 32 位比特串状态；按整数暴露，change 标志不改变值语义。
        bits = data.value.value
        result = 0
        for position, bit in enumerate(bits):
            if bit:
                result |= 1 << position
        return result, quality
    raise ValueError(f"unsupported IEC104 monitoring data type: {type(data).__name__}")


def decode_quality(quality: object) -> Quality:
    """hat 品质位 → wind-hub GOOD/BAD/UNCERTAIN。

    invalid → BAD；non_topical/substituted/blocked/overflow/adjusted →
    UNCERTAIN。品质对象缺失（理论上不应出现）按 UNCERTAIN 处理，
    绝不当 GOOD。
    """
    if quality is None:
        return Quality.UNCERTAIN
    if bool(getattr(quality, "invalid", False)):
        return Quality.BAD
    uncertain_flags = (
        "not_topical",
        "substituted",
        "blocked",
        "overflow",
        "adjusted",
    )
    if any(bool(getattr(quality, flag, False)) for flag in uncertain_flags):
        return Quality.UNCERTAIN
    return Quality.GOOD


def build_command(mapped: IEC104Point, value: object) -> iec101.Command:
    """按 type_id 把写值严格转换为 hat 命令对象。

    Raises:
        ConfigError: type_id 缺失或不是受支持的控制类型。
        TypeError: 值的 Python 类型与协议类型不匹配。
        ValueError: 值超出协议范围。
    """
    type_id = mapped.type_id
    if type_id is None:
        raise ConfigError(f"IEC104 writable point '{mapped.point_id}' requires type_id")

    if type_id == "C_SC_NA_1":
        if type(value) is not bool:
            raise TypeError("C_SC_NA_1 requires boolean value")
        return iec101.SingleCommand(
            value=iec101.SingleValue.ON if value else iec101.SingleValue.OFF,
            select=False,
            qualifier=0,
        )

    if type_id == "C_DC_NA_1":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("C_DC_NA_1 requires double-point value 1 (OFF) or 2 (ON)")
        integer = int(value)
        if float(value) != float(integer) or integer not in (1, 2):
            raise ValueError("C_DC_NA_1 requires 1 (OFF) or 2 (ON)")
        return iec101.DoubleCommand(
            value=iec101.DoubleValue.OFF if integer == 1 else iec101.DoubleValue.ON,
            select=False,
            qualifier=0,
        )

    if type_id == "C_SE_NA_1":
        number = _numeric(value, type_id)
        if not -1.0 <= number <= 1.0:
            raise ValueError("C_SE_NA_1 normalized value must be in -1..1")
        return iec101.NormalizedCommand(
            value=iec101.NormalizedValue(number), select=False
        )

    if type_id == "C_SE_NB_1":
        integer = _integer(value, type_id)
        if not -32768 <= integer <= 32767:
            raise ValueError("C_SE_NB_1 scaled value must be in int16 range")
        return iec101.ScaledCommand(value=iec101.ScaledValue(integer), select=False)

    if type_id == "C_SE_NC_1":
        return iec101.FloatingCommand(
            value=iec101.FloatingValue(_numeric(value, type_id)), select=False
        )

    raise ConfigError(
        f"IEC104 point '{mapped.point_id}' uses unsupported command type '{type_id}'"
    )


def negative_read_message(cause: object) -> str:
    """把 hat 的否定读取 COT 转为可读失败原因。"""
    name = getattr(cause, "name", str(cause))
    return f"IEC104 station rejected active read ({name})"


def _numeric(value: object, type_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{type_id} requires numeric value")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{type_id} requires a finite numeric value")
    return number


def _integer(value: object, type_id: str) -> int:
    number = _numeric(value, type_id)
    integer = int(number)
    if number != float(integer):
        raise ValueError(f"{type_id} requires integer value")
    return integer
