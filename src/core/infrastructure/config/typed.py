"""按主题返回已验证的类型化配置对象。

此层不解析跨文件业务引用，也不管理进程运行时状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel

from core.application import ConfigError
from core.application.config_types import (
    ADSLocalConfig,
    DeviceConfig,
    DeviceInstanceDefinition,
    DeviceInstancesConfig,
    DeviceModelDefinition,
    DeviceModelsConfig,
    DeviceTypeDefinition,
    EndpointDefinition,
    PointConfig,
    PointDefinition,
    PointTableDefinition,
    RuntimeSettings,
    SiteIdentity,
    SystemConfig,
    TaskConfig,
    TaskDefinition,
    UnitConfig,
    UnitDefinition,
)
from core.application.sink_config import SinksConfig
from core.infrastructure.config.point_tables import resolve_point_tables
from core.infrastructure.config.raw import (
    ADSSystemRaw,
    DeviceInstancesFile,
    DeviceModelsFile,
    PointTablesFile,
    TasksFile,
    UnitsFile,
)
from core.infrastructure.config.yaml import YamlConfigReader

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _freeze_config(value: Any) -> Any:
    """递归复制并冻结系统配置，阻止对共享配置的原地修改。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_config(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_config(item) for item in value)
    return value


_RUNTIME_KEYS = frozenset(
    {
        "queue_maxsize",
        "backpressure_policy",
        "shutdown_timeout",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
    }
)
_BACKPRESSURE_POLICIES = frozenset({"drop_old", "drop_new", "block"})
_TIMEOUT_KEYS = ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout")


def _parse_site(raw: Any) -> SiteIdentity | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("system.yaml 'site' must be a mapping")
    unknown = set(raw) - {"site_id", "name"}
    if unknown:
        raise ConfigError(f"system.yaml 'site' has unknown keys: {sorted(unknown)}")
    site_id = raw.get("site_id")
    name = raw.get("name")
    if not isinstance(site_id, str) or not site_id.strip():
        raise ConfigError("system.yaml 'site.site_id' must be a non-empty string")
    if name is not None and not isinstance(name, str):
        raise ConfigError("system.yaml 'site.name' must be a string")
    return SiteIdentity(site_id=site_id, name=name)


def _parse_runtime(raw: Any) -> RuntimeSettings:
    if raw is None:
        return RuntimeSettings()
    if not isinstance(raw, dict):
        raise ConfigError("system.yaml 'runtime' must be a mapping")
    unknown = set(raw) - _RUNTIME_KEYS
    if unknown:
        raise ConfigError(f"system.yaml 'runtime' has unknown keys: {sorted(unknown)}")

    queue_maxsize = raw.get("queue_maxsize")
    if queue_maxsize is not None:
        if isinstance(queue_maxsize, bool) or not isinstance(queue_maxsize, int):
            raise ConfigError("system.yaml 'runtime.queue_maxsize' must be an integer")
        if queue_maxsize <= 0:
            raise ConfigError("system.yaml 'runtime.queue_maxsize' must be > 0")

    policy = raw.get("backpressure_policy")
    if policy is not None and policy not in _BACKPRESSURE_POLICIES:
        raise ConfigError(
            f"Invalid backpressure_policy '{policy}'; "
            f"must be one of {sorted(_BACKPRESSURE_POLICIES)}"
        )

    timeouts: dict[str, float | None] = {}
    for key in _TIMEOUT_KEYS:
        value = raw.get(key)
        if value is None:
            timeouts[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"system.yaml 'runtime.{key}' must be a number")
        if value <= 0:
            raise ConfigError(f"system.yaml 'runtime.{key}' must be > 0")
        timeouts[key] = float(value)

    return RuntimeSettings(
        queue_maxsize=queue_maxsize,
        backpressure_policy=policy,
        shutdown_timeout=timeouts["shutdown_timeout"],
        connect_timeout=timeouts["connect_timeout"],
        read_timeout=timeouts["read_timeout"],
        write_timeout=timeouts["write_timeout"],
    )


def _parse_ads(raw: Any) -> ADSLocalConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("system.yaml 'ads' must be a mapping")
    try:
        parsed = ADSSystemRaw.model_validate(raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid system.ads configuration: {exc}") from exc
    return ADSLocalConfig(
        local_ams_net_id=parsed.local_ams_net_id,
        local_ip=parsed.local_ip,
        username=parsed.username,
        password=parsed.password,
    )


class YamlTypedConfigAdapter(YamlConfigReader):
    """复用基础 YAML 读取器，向调用方提供类型化配置。"""

    @staticmethod
    def _validate(model: type[_ModelT], raw: Mapping[str, Any], name: str) -> _ModelT:
        try:
            return model.model_validate(raw)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"Invalid {name} configuration: {exc}") from exc

    def read_system_config(self) -> SystemConfig:
        raw = self.read_system()
        return SystemConfig(
            site=_parse_site(raw.get("site")),
            runtime=_parse_runtime(raw.get("runtime")),
            ads=_parse_ads(raw.get("ads")),
        )

    def read_device_models_config(self) -> DeviceModelsConfig:
        definition = self._validate(DeviceModelsFile, self.read_device_models(), "device_models")
        return DeviceModelsConfig(
            device_types=MappingProxyType({
                key: DeviceTypeDefinition(name=value.name)
                for key, value in definition.device_types.items()
            }),
            device_models=MappingProxyType({
                key: DeviceModelDefinition(
                    device_type=value.device_type,
                    manufacturer=value.manufacturer,
                    model=value.model,
                    protocol=value.protocol,
                    point_table=value.point_table,
                    read_mode=value.read_mode,
                    properties=_freeze_config(value.properties),
                    connection_defaults=_freeze_config(value.connection_defaults),
                )
                for key, value in definition.device_models.items()
            }),
        )

    def read_device_instances_config(self) -> DeviceInstancesConfig:
        definition = self._validate(DeviceInstancesFile, self.read_devices(), "devices")
        return DeviceInstancesConfig(
            devices=tuple(
                DeviceInstanceDefinition(
                    device_id=item.device_id,
                    model=item.model,
                    device_group=item.device_group,
                    endpoint=EndpointDefinition(
                        host=item.endpoint.host,
                        port=item.endpoint.port,
                        extensions=_freeze_config(item.endpoint.extensions),
                    ),
                    enabled=item.enabled,
                )
                for item in definition.devices
            )
        )

    def read_device_config(self) -> DeviceConfig:
        return DeviceConfig(
            models=self.read_device_models_config(),
            instances=self.read_device_instances_config(),
        )

    def read_point_config(self) -> PointConfig:
        definition = self._validate(PointTablesFile, self.read_points(), "points")
        tables = resolve_point_tables(definition.point_tables)
        return PointConfig(
            tables=MappingProxyType({
                name: PointTableDefinition(
                    protocol=table.protocol,
                    points=MappingProxyType({
                        point_id: PointDefinition(
                            point_id=point.point_id,
                            variable_name=point.variable_name,
                            point_groups=tuple(point.point_groups),
                            address=_freeze_config(point.address.model_dump(exclude_none=True)),
                            data_type=point.data_type,
                            scale=point.scale,
                            offset=point.offset,
                            unit=point.unit,
                            description=point.description,
                        )
                        for point_id, point in table.points.items()
                    }),
                )
                for name, table in tables.items()
            })
        )

    def read_task_config(self) -> TaskConfig:
        definition = self._validate(TasksFile, self.read_tasks(), "tasks")
        return TaskConfig(
            tasks=tuple(
                TaskDefinition(
                    task_id=task.task_id,
                    device=task.device,
                    device_group=task.device_group,
                    point_group=task.point_group,
                    interval=task.interval,
                    targets=tuple(target.sink for target in task.targets),
                    enabled=task.enabled,
                )
                for task in definition.tasks
            )
        )

    def read_sink_config(self) -> SinksConfig:
        return self._validate(SinksConfig, self.read_sinks(), "sinks")

    def read_unit_config(self) -> UnitConfig:
        definition = self._validate(UnitsFile, self.read_units(), "units")
        units = {
            unit_id: UnitDefinition(symbol=raw.symbol, name=raw.name)
            for unit_id, raw in definition.units.items()
        }
        return UnitConfig(units=MappingProxyType(units))


__all__ = [
    "DeviceConfig",
    "PointConfig",
    "TaskConfig",
    "SystemConfig",
    "UnitConfig",
    "YamlTypedConfigAdapter",
]
