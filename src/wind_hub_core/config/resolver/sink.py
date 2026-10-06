"""Sink source 引用解析。

把 sinks.yaml 的 Raw SinkPoint 与已解析 Device/PointTable 关联，生成 Runtime
直接消费的 ResolvedSinkPoint。这里只做纯配置解析，不创建 Sink 资源，也不处理
实时 PointValue。
"""

from __future__ import annotations

from wind_hub_core.config.model.device import DeviceConfig
from wind_hub_core.config.model.point import PointConfig, ResolvedPointTable
from wind_hub_core.config.model.sink import (
    MODBUS_WORD_WIDTH,
    SINK_NUMERIC_DATA_TYPES,
    IEC104SinkAddress,
    ModbusSinkAddress,
    ResolvedSinkConfig,
    ResolvedSinkPoint,
    SinkConfig,
    SinkPoint,
    SinksConfig,
)
from wind_hub_core.config.model.unit import UnitsConfig
from wind_hub_core.model.errors import ConfigError

_MODBUS_BIT_REGISTER_TYPES = frozenset({"coil", "discrete"})


def resolve_sinks(
    raw: SinksConfig,
    devices: dict[str, DeviceConfig],
    point_tables: dict[str, ResolvedPointTable],
    units: UnitsConfig,
) -> dict[str, ResolvedSinkConfig]:
    """解析全部 Sink source 引用并补全稳定外部点元数据。

    Sink name 唯一性已在 :class:`SinksConfig` 解析时校验，resolve 保持
    name 不变，因此按 name 构造 dict 不会静默覆盖。
    """
    resolved: dict[str, ResolvedSinkConfig] = {}

    for sink in raw.sinks:
        points = [
            _resolve_point(sink, point, devices, point_tables, units)
            for point in sink.points
        ]
        _validate_modbus_layout(sink.name, points)
        resolved[sink.name] = ResolvedSinkConfig(
            name=sink.name,
            type=sink.type,
            enabled=sink.enabled,
            connection=sink.connection,
            points=points,
        )

    return resolved


def _resolve_point(
    sink: SinkConfig,
    point: SinkPoint,
    devices: dict[str, DeviceConfig],
    point_tables: dict[str, ResolvedPointTable],
    units: UnitsConfig,
) -> ResolvedSinkPoint:
    device = devices.get(point.source.device_id)
    if device is None:
        raise ConfigError(
            f"Sink '{sink.name}' point '{_raw_ref(point)}' references unknown device "
            f"'{point.source.device_id}'"
        )

    table = point_tables.get(device.point_table)
    if table is None:
        raise ConfigError(
            f"Sink '{sink.name}' point '{_raw_ref(point)}' references device "
            f"'{device.device_id}' with unknown point_table '{device.point_table}'"
        )
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
    _validate_iec104_type(sink.name, ref, point, datatype)

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


def _validate_iec104_type(
    sink_name: str,
    ref: str,
    point: SinkPoint,
    datatype: str,
) -> None:
    """校验 IEC104 TypeID 与导出 datatype 的基本值域类型一致性。"""
    address = point.address
    if not isinstance(address, IEC104SinkAddress):
        return

    if address.type_id in {"M_SP_NA_1", "M_SP_TB_1"}:
        if datatype != "bool":
            raise ConfigError(
                f"Sink '{sink_name}' point '{ref}': {address.type_id} requires "
                f"datatype 'bool', got '{datatype}'"
            )
        return

    if address.type_id in {"M_DP_NA_1", "M_DP_TB_1"}:
        integer_types = {
            "int8",
            "int16",
            "int32",
            "int64",
            "uint8",
            "uint16",
            "uint32",
            "uint64",
        }
        if datatype not in integer_types:
            raise ConfigError(
                f"Sink '{sink_name}' point '{ref}': {address.type_id} requires "
                f"integer datatype, got '{datatype}'"
            )
        return

    if datatype not in SINK_NUMERIC_DATA_TYPES:
        raise ConfigError(
            f"Sink '{sink_name}' point '{ref}': {address.type_id} requires numeric "
            f"datatype, got '{datatype}'"
        )


def _validate_modbus_layout(
    sink_name: str,
    points: list[ResolvedSinkPoint],
) -> None:
    """校验 Modbus Sink 点的数据类型、寄存器跨度与地址重叠。"""
    occupied: dict[
        tuple[int, str],
        list[tuple[int, int, str]],
    ] = {}

    for point in points:
        address = point.address
        if not isinstance(address, ModbusSinkAddress):
            continue

        width: int | None
        if address.register_type in _MODBUS_BIT_REGISTER_TYPES:
            if point.datatype != "bool":
                raise ConfigError(
                    f"Sink '{sink_name}' point '{point.ref}': "
                    f"{address.register_type} requires datatype 'bool', "
                    f"got '{point.datatype}'"
                )
            width = 1
        else:
            width = MODBUS_WORD_WIDTH.get(point.datatype)
            if width is None:
                raise ConfigError(
                    f"Sink '{sink_name}' point '{point.ref}': "
                    f"{address.register_type} does not support datatype "
                    f"'{point.datatype}'"
                )

        end = address.address + width - 1
        if end > 0xFFFF:
            raise ConfigError(
                f"Sink '{sink_name}' point '{point.ref}': Modbus address range "
                f"{address.address}..{end} exceeds 65535"
            )

        key = (address.unit_id, address.register_type)
        occupied.setdefault(key, []).append(
            (address.address, end, point.ref)
        )

    for (unit_id, register_type), ranges in occupied.items():
        ranges.sort(key=lambda item: item[0])
        for previous, current in zip(ranges, ranges[1:], strict=False):
            prev_start, prev_end, prev_ref = previous
            cur_start, cur_end, cur_ref = current
            if cur_start <= prev_end:
                raise ConfigError(
                    f"Sink '{sink_name}': Modbus address overlap on unit "
                    f"{unit_id} {register_type}: '{prev_ref}' "
                    f"[{prev_start}..{prev_end}] overlaps '{cur_ref}' "
                    f"[{cur_start}..{cur_end}]"
                )


def _raw_ref(point: SinkPoint) -> str:
    return point.ref or f"{point.source.device_id}.{point.source.point_id}"


__all__ = ["resolve_sinks"]
