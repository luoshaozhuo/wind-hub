"""Collector 完整领域配置快照加载入口。"""

from __future__ import annotations

from typing import cast

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.port import ConfigPort
from core.application.settings import RuntimeSettings
from core.domain import Device, DeviceId, ProtocolOptions, Sink
from core.domain.unit import UNIT_CATALOG, UnitCode
from core.infrastructure.config.effective import resolve_effective_device
from core.infrastructure.config.sink_schema import (
    MODBUS_WORD_WIDTH,
    SINK_NUMERIC_DATA_TYPES,
    ModbusSinkAddress,
    ResolvedSinkPoint,
)

from ...application.config import BackpressurePolicy, CollectorConfig, RuntimeParams


def load_collector_config(source: ConfigPort) -> CollectorConfig:
    """从注入的配置来源加载 Collector 配置；不在 Loader 内实例化 Adapter。"""
    return _from_snapshot(source.load())


def _from_snapshot(snapshot: ConfigSnapshot) -> CollectorConfig:
    enabled_sinks = {sink_id for sink_id, sink in snapshot.sinks.items() if sink.enabled}
    for task in snapshot.tasks.values():
        if not task.enabled:
            continue
        unavailable = set(task.sink_ids) - enabled_sinks
        if unavailable:
            raise ConfigError(
                f"task '{task.task_id}' references disabled sinks: {sorted(unavailable)}"
            )
    devices = snapshot.site.devices
    settings = snapshot.system.runtime
    effective_devices: dict[DeviceId, Device] = {}
    effective_options: dict[DeviceId, ProtocolOptions] = {}
    for key, device in devices.items():
        if not device.enabled:
            continue
        model = snapshot.device_models[device.device_model_id]
        protocol = snapshot.point_tables[model.point_table_id].protocol.name
        merged_device, merged_options = resolve_effective_device(
            device,
            model,
            protocol,
            snapshot.protocol_options_by_device.get(key, {}),
        )
        effective_devices[key] = merged_device
        effective_options[key] = merged_options
    return CollectorConfig(
        devices=effective_devices,
        device_models=snapshot.device_models,
        point_tables=snapshot.point_tables,
        protocol_options_by_device=effective_options,
        runtime=_runtime_params(settings),
        ads_local=snapshot.system.ads,
        tasks=snapshot.tasks,
        sinks=_resolve_sinks(snapshot),
        ads_subscribe_devices=snapshot.ads_subscribe_devices,
        disabled_devices=frozenset(
            DeviceId(key) for key, device in devices.items() if not device.enabled
        ),
    )


def _resolve_sinks(snapshot: ConfigSnapshot) -> dict[str, Sink]:
    """解析外部 Sink 点引用，明确源类型与单位，不依赖 units.yaml。"""
    result: dict[str, Sink] = {}
    for sink_id, sink in snapshot.sinks.items():
        points = []
        refs: set[str] = set()
        addresses: dict[tuple[int, str], list[tuple[int, int]]] = {}
        for raw in sink.points:
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
            source_data_type = point.ext["data_type"]
            source_unit = point.source_unit.code.value
            unit = raw.get("unit") or source_unit
            try:
                UNIT_CATALOG[UnitCode(unit)]
            except (KeyError, ValueError) as exc:
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
                key = (address.unit_id, address.register_type)
                occupied = addresses.setdefault(key, [])
                if any(start <= end and address.address <= finish for start, finish in occupied):
                    raise ConfigError(
                        f"Sink '{sink_id}' point '{resolved.ref}': "
                        "overlapping Modbus register addresses"
                    )
                occupied.append((address.address, end))
            points.append(resolved.model_dump(mode="json"))
        result[sink_id] = Sink(
            sink_id=sink.sink_id,
            kind=sink.kind,
            enabled=sink.enabled,
            connection=sink.connection,
            points=tuple(points),
        )
    return result


def _runtime_params(settings: RuntimeSettings) -> RuntimeParams:
    return RuntimeParams(
        queue_maxsize=settings.queue_maxsize if settings.queue_maxsize is not None else 1000,
        backpressure_policy=cast(BackpressurePolicy, settings.backpressure_policy or "drop_old"),
        shutdown_timeout=(
            settings.shutdown_timeout if settings.shutdown_timeout is not None else 10.0
        ),
        connect_timeout=settings.connect_timeout if settings.connect_timeout is not None else 1.0,
        read_timeout=settings.read_timeout if settings.read_timeout is not None else 1.0,
        write_timeout=settings.write_timeout if settings.write_timeout is not None else 1.0,
        watchdog_interval=settings.watchdog_interval,
        health_timeout=settings.health_timeout,
    )


__all__ = ["load_collector_config"]
