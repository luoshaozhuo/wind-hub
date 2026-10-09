"""Commander 现场配置加载入口。

通过 Core 统一配置服务获取本进程所需主题（system / device_models /
devices / points / units）的配置 VO，完成 Core 快照组装与跨文件
一致性校验，产出 CommanderConfig。Commander 不读取 TASKS/SINKS。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

from core.application import ConfigError, ConfigService
from core.application.port import ConfigTopic, ConfigValue
from core.domain.config import (
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    SystemConfig,
    UnitsConfig,
)
from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.config.assembly import assemble_core_config

from ...application.config import (
    COMMANDER_CONFIG_TOPICS,
    ADSLocalIdentity,
    CommanderConfig,
    PointMeta,
)


class _ConfigSource(Protocol):
    """配置来源：ConfigPort 或一致性快照（均有 ``read(topic)``）。"""

    def read(self, topic: ConfigTopic) -> ConfigValue: ...


def load_commander_config(
    config_dir: str | Path, *, source: _ConfigSource | None = None
) -> CommanderConfig:
    """加载 Commander 配置并完成跨文件一致性校验。

    默认在一致性快照内读取 Commander 消费的主题；tasks/sinks 等
    无关文件不解析，加载期间的外部并发修改经 ``verify_unchanged``
    检测并中止。

    Raises:
        ConfigError: 文件缺失、YAML 非法、加载期间配置被修改或任何
            配置约束违反。
    """
    if source is not None:
        return _load_from(source)
    adapter = YamlConfigAdapter(config_dir)
    service = ConfigService(adapter, snapshots=adapter)
    snapshot = service.open_snapshot(COMMANDER_CONFIG_TOPICS)
    config = _load_from(snapshot)
    snapshot.verify_unchanged()
    return config


def _load_from(source: _ConfigSource) -> CommanderConfig:
    system = cast(SystemConfig, source.read(ConfigTopic.SYSTEM))
    models = cast(DeviceModelsConfig, source.read(ConfigTopic.DEVICE_MODELS))
    devices = cast(DevicesConfig, source.read(ConfigTopic.DEVICES))
    points = cast(PointTablesConfig, source.read(ConfigTopic.POINTS))
    units = cast(UnitsConfig, source.read(ConfigTopic.UNITS))

    connect_timeout = (
        system.runtime.connect_timeout if system.runtime.connect_timeout is not None else 10.0
    )
    write_timeout = (
        system.runtime.write_timeout if system.runtime.write_timeout is not None else 5.0
    )
    if connect_timeout <= 0:
        raise ConfigError("runtime.connect_timeout must be > 0")
    if write_timeout <= 0:
        raise ConfigError("runtime.write_timeout must be > 0")

    assembly = assemble_core_config(
        device_models_config=models,
        devices_config=devices,
        point_config=points,
        unit_config=units,
    )

    return CommanderConfig(
        devices=assembly.devices,
        device_models=assembly.device_models,
        device_groups=assembly.device_groups,
        point_tables=assembly.point_tables,
        business_points=assembly.business_points,
        protocol_options_by_device=assembly.protocol_options_by_device,
        point_meta={
            table_id: {
                point_id: PointMeta(
                    variable_name=meta.variable_name,
                    point_groups=meta.point_groups,
                )
                for point_id, meta in table_points.items()
            }
            for table_id, table_points in assembly.point_meta.items()
        },
        ads_local=(
            ADSLocalIdentity(
                local_ams_net_id=system.ads.local_ams_net_id,
                local_ip=system.ads.local_ip,
            )
            if system.ads is not None
            else None
        ),
        connect_timeout=connect_timeout,
        write_timeout=write_timeout,
        read_timeout=system.runtime.read_timeout if system.runtime.read_timeout is not None else 5.0,
        reconnect_attempts=system.runtime.reconnect_attempts,
        disabled_devices=assembly.disabled_devices,
    )


__all__ = ["load_commander_config"]
