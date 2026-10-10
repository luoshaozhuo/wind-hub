"""Commander 现场配置加载入口。

经 :class:`ConfigPort` 按主题读取本进程所需配置 VO（system /
device_models / devices / points / units），完成 Core 配置组装与
跨文件一致性校验，产出 CommanderConfig。Commander 不读取 TASKS/SINKS。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from core.application import ConfigError
from core.application.port import ConfigPort, ConfigTopic
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
    ADSLocalIdentity,
    CommanderConfig,
    PointMeta,
)


def load_commander_config(
    config_dir: str | Path, *, source: ConfigPort | None = None
) -> CommanderConfig:
    """加载 Commander 配置并完成跨文件一致性校验。

    默认经 :class:`YamlConfigAdapter` 按主题读取；tasks/sinks 等无关
    文件不解析。``source`` 允许测试注入任意 ConfigPort 实现。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    return _load_from(source if source is not None else YamlConfigAdapter(config_dir))


def _load_from(source: ConfigPort) -> CommanderConfig:
    system = cast(SystemConfig, source.read(ConfigTopic.SYSTEM))
    models = cast(DeviceModelsConfig, source.read(ConfigTopic.DEVICE_MODELS))
    devices = cast(DevicesConfig, source.read(ConfigTopic.DEVICES))
    points = cast(PointTablesConfig, source.read(ConfigTopic.POINTS))
    units = cast(UnitsConfig, source.read(ConfigTopic.UNITS))

    connect_timeout = (
        system.runtime.connect_timeout if system.runtime.connect_timeout is not None else 1.0
    )
    write_timeout = (
        system.runtime.write_timeout if system.runtime.write_timeout is not None else 1.0
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
        read_timeout=(
            system.runtime.read_timeout if system.runtime.read_timeout is not None else 1.0
        ),
        read_retries=system.runtime.read_retries,
        retry_interval=system.runtime.retry_interval,
        disabled_devices=assembly.disabled_devices,
    )


__all__ = ["load_commander_config"]
