"""Commander 现场配置加载入口。

通过 Core 类型化配置读取端口获取本进程所需主题（system / device /
points / units），完成 Core 快照组装与跨文件一致性校验，产出
CommanderConfig。
"""

from __future__ import annotations

from pathlib import Path

from core.application import ConfigError
from core.application.port.typed_config import TypedConfigReader
from core.infrastructure.config import YamlTypedConfigAdapter

from ...application.config import ADSLocalIdentity, CommanderConfig
from .snapshot import build_core_snapshot


def load_commander_config(
    config_dir: str | Path, *, reader: TypedConfigReader | None = None
) -> CommanderConfig:
    """加载 Commander 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    reader = reader if reader is not None else YamlTypedConfigAdapter(config_dir)

    system = reader.read_system_config()
    device_config = reader.read_device_config()
    point_config = reader.read_point_config()
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

    snapshot, point_meta, disabled = build_core_snapshot(
        device_config=device_config,
        point_config=point_config,
        unit_config=unit_config,
    )

    return CommanderConfig(
        core=snapshot,
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
        point_meta=point_meta,
        disabled_devices=disabled,
    )


__all__ = ["load_commander_config"]
