"""Resolve Sink-specific address mappings outside Collector."""
from __future__ import annotations

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.sink_contract import (
    GrpcSinkAddress,
    ModbusSinkAddress,
    ResolvedSink,
    ResolvedSinkPoint,
)
from core.domain import DeviceId, Sink, Task
from core.infrastructure.config.sink_schema import MODBUS_WORD_WIDTH, SINK_NUMERIC_DATA_TYPES

def _task_group_sources(snapshot: ConfigSnapshot, task: Task) -> tuple[tuple[str, str], ...]:
    """展开空 points 输出：任务设备组 × 测点组内的全部 (device, point)。"""
    sources: list[tuple[str, str]] = []
    for device in snapshot.site.devices.values():
        if not device.enabled or task.device_group_id not in device.device_group_ids:
            continue
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        for point_id, point in table.points.items():
            if task.point_group in point.point_groups:
                sources.append((str(device.device_id), point_id))
    return tuple(sources)


def _resolve_sinks(snapshot: ConfigSnapshot) -> dict[str, Sink]:
    """解析外部 Sink 点引用，明确源类型与单位（单位由快照单位目录解析）。

    只聚合**启用 Task** 的 outputs：禁用 Task 不部署，其点不得占用 Modbus
    寄存器/白名单，其过期设备引用也不得阻断启动（结构正确性在解析层仍对
    全部 Task 校验，保证重新启用时配置可用）。
    """
    result: dict[str, Sink] = {}
    enabled_tasks = [task for task in snapshot.tasks.values() if task.enabled]
    for sink_id, sink in snapshot.sinks.items():
        points = []
        task_outputs = [
            (task, output)
            for task in enabled_tasks
            for output in task.outputs
            if output.sink_id == sink_id
        ]
        task_points = [raw for _, output in task_outputs for raw in output.points]
        refs: set[str] = set()
        addresses: dict[tuple[int, str], list[tuple[int, int]]] = {}
        grpc_destinations: set[tuple[str, str]] = set()
        grpc_sources: set[tuple[str, str]] = set()
        for raw in task_points:
            source = raw["source"]
            device_id = DeviceId(source["device_id"])
            device = snapshot.site.devices.get(device_id)
            if device is None:
                raise ConfigError(f"Sink '{sink_id}' references unknown device '{device_id}'")
            model = snapshot.device_models[device.device_model_id]
            table = snapshot.point_tables[model.point_table_id]
            point = table.points.get(source["point_id"])
            if point is None:
                raise ConfigError(
                    f"Sink '{sink_id}' references unknown point '{source['point_id']}'"
                )
            # Sink 边界只认统一逻辑数据类型（float32/int32/...）；
            # point.ext["data_type"] 是协议原生表示（如 ADS 的 REAL），
            # 仅供 Driver 编解码，不能传给 Sink Schema。领域加载已保证
            # Point 必然引用存在的业务点，且两者逻辑类型一致。
            source_data_type = snapshot.business_points[point.business_point_id].data_type.value
            source_unit = point.source_unit.unit_id
            unit = raw.get("unit") or source_unit
            try:
                snapshot.units.unit(unit)
            except ValueError as exc:
                raise ConfigError(f"Sink '{sink_id}' unknown unit '{unit}'") from exc
            resolved = ResolvedSinkPoint.model_validate(
                {
                    **raw,
                    "ref": raw.get("ref") or f"{device_id}.{point.point_id}",
                    "source_data_type": source_data_type,
                    "source_unit": source_unit,
                    "datatype": raw.get("datatype") or source_data_type,
                    "unit": unit,
                }
            )
            if resolved.ref in refs:
                raise ConfigError(f"Sink '{sink_id}' duplicate point ref '{resolved.ref}'")
            refs.add(resolved.ref)
            if (resolved.scale != 1.0 or resolved.offset != 0.0) and (
                resolved.source_data_type not in SINK_NUMERIC_DATA_TYPES
                or resolved.datatype not in SINK_NUMERIC_DATA_TYPES
            ):
                raise ConfigError(
                    f"Sink '{sink_id}' point '{resolved.ref}': "
                    "scale/offset require numeric source and datatype"
                )
            if isinstance(resolved.address, GrpcSinkAddress):
                destination = (resolved.address.device_id, resolved.address.point_id)
                source_key = (resolved.source.device_id, resolved.source.point_id)
                if destination in grpc_destinations or source_key in grpc_sources:
                    raise ConfigError(
                        f"Sink '{sink_id}' duplicate gRPC source or destination: {resolved.ref}"
                    )
                grpc_destinations.add(destination)
                grpc_sources.add(source_key)
            if isinstance(resolved.address, ModbusSinkAddress):
                address = resolved.address
                is_bit = address.register_type in {"coil", "discrete"}
                if is_bit and resolved.datatype != "bool":
                    raise ConfigError(
                        f"Sink '{sink_id}' point '{resolved.ref}': "
                        f"{address.register_type} requires bool"
                    )
                width = 1 if is_bit else MODBUS_WORD_WIDTH.get(resolved.datatype)
                if width is None:
                    raise ConfigError(
                        f"Sink '{sink_id}' point '{resolved.ref}': "
                        f"unsupported Modbus datatype '{resolved.datatype}'"
                    )
                end = address.address + width - 1
                if end > 65535:
                    raise ConfigError(
                        f"Sink '{sink_id}' point '{resolved.ref}': "
                        "Modbus address range exceeds 65535"
                    )
                key = (address.slave_id, address.register_type)
                occupied = addresses.setdefault(key, [])
                if any(start <= end and address.address <= finish for start, finish in occupied):
                    raise ConfigError(
                        f"Sink '{sink_id}' point '{resolved.ref}': "
                        "overlapping Modbus register addresses"
                    )
                occupied.append((address.address, end))
            points.append(resolved.model_dump(mode="json"))
        task_sources = None
        if sink.kind in {"modbus", "redis", "tdengine", "grpc"}:
            # 显式 points 是选择性输出；空 points 对 Redis/TDengine 表示任务组全量
            # （Modbus 已在解析层强制显式点表，空列表不会走到这里）。
            task_sources = {
                task.task_id: (
                    tuple(
                        (raw["source"]["device_id"], raw["source"]["point_id"])
                        for raw in output.points
                    )
                    if output.points
                    else _task_group_sources(snapshot, task)
                )
                for task, output in task_outputs
            }
        task_key_prefixes = None
        if sink.kind == "redis":
            task_key_prefixes = {
                task.task_id: output.key_prefix or sink.connection.get("key_prefix", "wind-hub")
                for task, output in task_outputs
            }
        result[sink_id] = ResolvedSink(
            sink_id=sink.sink_id,
            kind=sink.kind,
            enabled=sink.enabled,
            connection=sink.connection,
            points=tuple(points),
            task_sources=task_sources,
            task_key_prefixes=task_key_prefixes,
            task_refs=(
                {
                    task.task_id: tuple(
                        raw.get("ref")
                        or f"{raw['source']['device_id']}.{raw['source']['point_id']}"
                        for raw in output.points
                    )
                    for task, output in task_outputs
                }
                if sink.kind == "modbus"
                else None
            ),
        )
    return result


