"""协议点值解释与业务归一化。

本模块把 ProtocolSample 转换为稳定 PointValue：
1. 按 Device -> DeviceModel -> PointTable 解析 ProtocolPoint；
2. 应用 ProtocolPoint.scale / offset 得到 source_unit 下的工程值；
3. 从 source_unit 换算到 BusinessPoint.standard_unit；
4. 按 BusinessPoint.value_type 校验并收敛最终标量类型。

这里只处理纯内存业务映射，不执行协议 I/O。
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite

from .config import CoreCoreConfigSnapshot
from core.domain import (
    BusinessPoint,
    DeviceId,
    ProtocolPoint,
    ValueType,
    convert_value,
)

from .measurement import PointScalar, PointValue, ProtocolSample


def interpret_protocol_sample(
    snapshot: CoreConfigSnapshot,
    device_id: DeviceId,
    sample: ProtocolSample,
    *,
    observed_at: datetime,
) -> PointValue:
    """把一条协议原始值解释为标准业务点值。

    Args:
        snapshot: 已通过一致性校验的静态配置快照。
        device_id: 产生该样本的设备。
        sample: Protocol Adapter 返回的原始样本。
        observed_at: Application 接收到样本的时间；当协议未提供时间戳时使用。

    Returns:
        使用 BusinessPoint 标准单位和标准值类型的不可变 PointValue。

    Raises:
        KeyError: Device、DeviceModel、PointTable、ProtocolPoint 或 BusinessPoint 不存在。
        ValueError: 时间戳、点映射、值类型、缩放或单位换算不合法。
    """
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    if sample.timestamp is not None and sample.timestamp.tzinfo is None:
        raise ValueError("protocol sample timestamp must be timezone-aware")

    device = snapshot.devices[device_id]
    model = snapshot.device_models[device.device_model_id]
    point_table = snapshot.point_tables[model.point_table_id]
    protocol_point = point_table.point(sample.point_id)
    business_point = snapshot.business_points[protocol_point.business_point_id]

    value = _interpret_value(sample.value, protocol_point, business_point)

    return PointValue(
        device_id=device.device_id,
        business_point_id=business_point.business_point_id,
        value=value,
        unit=business_point.standard_unit,
        quality=sample.quality,
        timestamp=sample.timestamp or observed_at,
        source_point_id=protocol_point.point_id,
    )


def _interpret_value(
    raw_value: PointScalar,
    protocol_point: ProtocolPoint,
    business_point: BusinessPoint,
) -> PointScalar:
    if raw_value is None:
        return None

    value_type = business_point.value_type

    if value_type is ValueType.BOOLEAN:
        _require_identity_mapping(protocol_point, value_type)
        if type(raw_value) is not bool:
            raise ValueError(
                f"business point '{business_point.business_point_id}' expects boolean, "
                f"got {type(raw_value).__name__}"
            )
        return raw_value

    if value_type is ValueType.STRING:
        _require_identity_mapping(protocol_point, value_type)
        if not isinstance(raw_value, str):
            raise ValueError(
                f"business point '{business_point.business_point_id}' expects string, "
                f"got {type(raw_value).__name__}"
            )
        return raw_value

    numeric_value = _require_numeric(raw_value, business_point)
    source_value = numeric_value * protocol_point.scale + protocol_point.offset
    if not isfinite(source_value):
        raise ValueError(
            f"business point '{business_point.business_point_id}' produced non-finite "
            "scaled value"
        )

    standard_value = convert_value(
        source_value,
        protocol_point.source_unit,
        business_point.standard_unit,
    )
    if not isfinite(standard_value):
        raise ValueError(
            f"business point '{business_point.business_point_id}' produced non-finite "
            "converted value"
        )

    if value_type is ValueType.FLOAT:
        return float(standard_value)

    if value_type is ValueType.INTEGER:
        if not standard_value.is_integer():
            raise ValueError(
                f"business point '{business_point.business_point_id}' expects integer, "
                f"got normalized value {standard_value}"
            )
        return int(standard_value)

    raise ValueError(
        f"unsupported business value type '{business_point.value_type}' "
        f"for '{business_point.business_point_id}'"
    )


def _require_numeric(
    value: PointScalar,
    business_point: BusinessPoint,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"business point '{business_point.business_point_id}' expects numeric value, "
            f"got {type(value).__name__}"
        )

    numeric_value = float(value)
    if not isfinite(numeric_value):
        raise ValueError(
            f"business point '{business_point.business_point_id}' received non-finite value"
        )
    return numeric_value


def _require_identity_mapping(
    protocol_point: ProtocolPoint,
    value_type: ValueType,
) -> None:
    if protocol_point.scale != 1.0 or protocol_point.offset != 0.0:
        raise ValueError(
            f"{value_type.value} point '{protocol_point.point_id}' must use "
            "scale=1 and offset=0"
        )
