"""Sink source 引用解析。

把 sinks.yaml 的 Raw SinkPoint 与已解析 Device/PointTable 关联，生成 Runtime
直接消费的 ResolvedSinkPoint。这里只做纯配置解析，不创建 Sink 资源，也不处理
实时 PointValue。
"""

from __future__ import annotations

from wind_hub_core.config.schema import (
    DeviceConfig,
    DevicesConfig,
    PointConfig,
    ResolvedPointTables,
    UnitsConfig,
)
from wind_hub_core.config.sinks import (
    ResolvedSinkConfig,
    ResolvedSinkPoint,
    ResolvedSinksConfig,
    SINK_NUMERIC_DATA_TYPES,
    SinkConfig,
    SinkPoint,
    SinksConfig,
)
from wind_hub_core.model.errors import ConfigError


def resolve_sinks(
    raw: SinksConfig,
    devices: DevicesConfig,
    point_tables: ResolvedPointTables,
    units: UnitsConfig,
) -> ResolvedSinksConfig:
    """解析全部 Sink source 引用并补全稳定外部点元数据。"""
    device_map = {device.device_id: device for device in devices.devices}
    resolved: list[ResolvedSinkConfig] = []

    for sink in raw.sinks:
        points = [
            _resolve_point(sink, point, device_map, point_tables, units)
            for point in sink.points
        ]
        resolved.append(
            ResolvedSinkConfig(
                name=sink.name,
                type=sink.type,
                enabled=sink.enabled,
                connection=sink.connection,
                points=points,
            )
        )

    return ResolvedSinksConfig(sinks=resolved)


def _resolve_point(
    sink: SinkConfig,
    point: SinkPoint,
    device_map: dict[str, DeviceConfig],
    point_tables: ResolvedPointTables,
    units: UnitsConfig,
) -> ResolvedSinkPoint:
    device = device_map.get(point.source.device_id)
    if device is None:
        raise ConfigError(
            f"Sink '{sink.name}' point '{_raw_ref(point)}' references unknown device "
            f"'{point.source.device_id}'"
        )

    table = point_tables.tables[device.point_table]
    source = next(
        (item for item in table.points if item.point_id == point.source.point_id),
        None,
    )
    if source is None:
        raise ConfigError(
            f"Sink '{sink.name}' point '{_raw_ref(point)}' references unknown point "
            f"'{point.source.point_id}' on device '{point.source.device_id}'"
        )

    ref = point.ref or f"{point.source.device_id}.{point.source.point_id}"
    datatype = point.datatype or source.data_type
    unit = point.unit or source.unit
    if unit not in units.units:
        raise ConfigError(
            f"Sink '{sink.name}' point '{ref}' references unknown unit '{unit}'"
        )

    _validate_transform(sink.name, ref, point, source, datatype)

    return ResolvedSinkPoint(
        source=point.source,
        ref=ref,
        source_data_type=source.data_type,
        source_unit=source.unit,
        datatype=datatype,
        unit=unit,
        scale=point.scale,
        offset=point.offset,
        address=point.address,
    )


def _validate_transform(
    sink_name: str,
    ref: str,
    point: SinkPoint,
    source: PointConfig,
    datatype: str,
) -> None:
    if point.scale == 1.0 and point.offset == 0.0:
        return
    if source.data_type not in SINK_NUMERIC_DATA_TYPES:
        raise ConfigError(
            f"Sink '{sink_name}' point '{ref}': scale/offset require numeric source, "
            f"got '{source.data_type}'"
        )
    if datatype not in SINK_NUMERIC_DATA_TYPES:
        raise ConfigError(
            f"Sink '{sink_name}' point '{ref}': scale/offset require numeric datatype, "
            f"got '{datatype}'"
        )


def _raw_ref(point: SinkPoint) -> str:
    return point.ref or f"{point.source.device_id}.{point.source.point_id}"


__all__ = ["resolve_sinks"]
