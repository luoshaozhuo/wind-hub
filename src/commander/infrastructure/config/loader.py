"""Commander 现场配置加载入口。

通过 Core 类型化配置读取端口获取本进程所需主题（system / device /
points / units），完成 Core 快照组装与跨文件一致性校验，产出
CommanderConfig。
"""

from __future__ import annotations

from pathlib import Path

from core.application import ConfigError
from core.application.port import ConfigReader
from core.infrastructure.config import YamlTypedConfigAdapter
from core.infrastructure.config.assembly import assemble_core_config

from ...application.config import ADSLocalIdentity, CommanderConfig, PointMeta


def load_commander_config(
    config_dir: str | Path, *, reader: ConfigReader | None = None
) -> CommanderConfig:
    """加载 Commander 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    reader = reader if reader is not None else YamlTypedConfigAdapter(config_dir)

    system = reader.read_system_config()
    device_config = reader.read_device_config()
    point_config = reader.read_point_tables_config()
    unit_config = reader.read_unit_config()

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
        device_config=device_config,
        point_config=point_config,
        unit_config=unit_config,
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
                for point_id, meta in points.items()
            }
            for table_id, points in assembly.point_meta.items()
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
        disabled_devices=assembly.disabled_devices,
    )


__all__ = ["load_commander_config"]
