"""Sink source 引用解析（Collector 侧，基于 CoreConfigAssembly）。

把 sinks.yaml 的 Raw SinkPoint 与 Core 快照中的 Device/PointTable 关联，
生成 Runtime 直接消费的 ResolvedSinkPoint。只做纯配置解析，不创建 Sink
资源，也不处理实时 PointValue。
"""

from __future__ import annotations

from collections.abc import Mapping

from core.application import ConfigError
from core.application.config_types import UnitConfig
from core.application.sink_config import (
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
from core.domain import DeviceId, Point, PointTable
from core.domain.config.lookups import point_table_for_device
from core.infrastructure.config.assembly import CoreConfigAssembly

_MODBUS_BIT_REGISTER_TYPES = frozenset({"coil", "discrete"})


def resolve_sinks(
    raw: SinksConfig,
    assembly: CoreConfigAssembly,
    unit_config: UnitConfig,
    disabled_tables: Mapping[str, PointTable] | None = None,
) -> dict[str, ResolvedSinkConfig]:
    """解析全部 Sink source 引用并补全稳定外部点元数据。

    Sink name 唯一性已在 :class:`SinksConfig` 解析时校验，resolve 保持
    name 不变，因此按 name 构造 dict 不会静默覆盖。``disabled_tables``
    提供 disabled 设备的点表查找（disabled 设备不进索引，但 Sink 引用
    合法，与旧行为一致）。
    """
    resolved: dict[str, ResolvedSinkConfig] = {}
    fallback = disabled_tables or {}

    for sink in raw.sinks:
        points = [
            _resolve_point(sink, point, assembly, unit_config, fallback) for point in sink.points
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
    assembly: CoreConfigAssembly,
    unit_config: UnitConfig,
    disabled_tables: Mapping[str, PointTable],
) -> ResolvedSinkPoint:
    device_id = DeviceId(point.source.device_id)
    if device_id in assembly.devices:
        table = point_table_for_device(
            assembly.devices, assembly.device_models, assembly.point_tables, device_id
        )
    elif point.source.device_id in disabled_tables:
        table = disabled_tables[point.source.device_id]
    else:
        raise ConfigError(
            f"Sink '{sink.name}' point '{_raw_ref(point)}' references unknown device "
            f"'{point.source.device_id}'"
        )
    source = table.points.get(point.source.point_id)
    if source is None:
        raise ConfigError(
            f"Sink '{sink.name}' point '{_raw_ref(point)}' references unknown point "
            f"'{point.source.point_id}' on device '{point.source.device_id}'"
        )

    source_data_type = _source_data_type(sink.name, point, source)
    source_unit = source.source_unit.code.value

    ref = point.ref or f"{point.source.device_id}.{point.source.point_id}"
    datatype = point.datatype or source_data_type
    unit = point.unit or source_unit
    if unit not in unit_config.units:
        raise ConfigError(f"Sink '{sink.name}' point '{ref}' references unknown unit '{unit}'")

    _validate_transform(sink.name, ref, point, source_data_type, datatype)
    _validate_iec104_type(sink.name, ref, point, datatype)

    return ResolvedSinkPoint(
        source=point.source,
        ref=ref,
        source_data_type=source_data_type,
        source_unit=source_unit,
        datatype=datatype,
        unit=unit,
        scale=point.scale,
        offset=point.offset,
        address=point.address,
    )


def _source_data_type(sink_name: str, point: SinkPoint, source: Point) -> str:
    """Core Point 的源 data_type（快照组装时写入 ``ext['data_type']``）。"""
    data_type = source.ext.get("data_type")
    if not isinstance(data_type, str):
        raise ConfigError(
            f"Sink '{sink_name}' point '{_raw_ref(point)}': source point "
            f"'{source.point_id}' has no data_type metadata"
        )
    return data_type


def _validate_transform(
    sink_name: str,
    ref: str,
    point: SinkPoint,
    source_data_type: str,
    datatype: str,
) -> None:
    if point.scale == 1.0 and point.offset == 0.0:
        return
    if source_data_type not in SINK_NUMERIC_DATA_TYPES:
        raise ConfigError(
            f"Sink '{sink_name}' point '{ref}': scale/offset require numeric source, "
            f"got '{source_data_type}'"
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
        occupied.setdefault(key, []).append((address.address, end, point.ref))

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
