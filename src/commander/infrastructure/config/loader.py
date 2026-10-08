"""Commander 现场配置加载入口。

读取同一现场配置目录中的 Commander 子集（system / device_models /
devices / points / units），完成 Raw 解析、点表继承展开、Core 快照组装
与跨文件一致性校验，产出 CommanderConfig。
"""

from __future__ import annotations

from pathlib import Path

from core.application import ConfigError
from core.application.port.config import CommanderConfigReader
from core.infrastructure.config import YamlTypedConfigAdapter

from ...application.config import ADSLocalIdentity, CommanderConfig
from .point_tables import resolve_point_tables
from .raw import (
    ADSSystemRaw,
    DeviceInstancesFile,
    DeviceModelsFile,
    PointTablesFile,
    UnitsFile,
)
from .snapshot import build_core_snapshot


def load_commander_config(
    config_dir: str | Path, *, reader: CommanderConfigReader | None = None
) -> CommanderConfig:
    """加载 Commander 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    reader = reader if reader is not None else YamlTypedConfigAdapter(config_dir)

    system_raw = reader.read_system()
    typed_reader = reader if isinstance(reader, YamlTypedConfigAdapter) else None
    typed = typed_reader is not None
    if typed_reader is not None:
        device_config = typed_reader.read_device_config()
        point_config = typed_reader.read_point_config()
        unit_config = typed_reader.read_unit_config()
    else:
        models_raw = reader.read_device_models()
        devices_raw = reader.read_devices()
        points_raw = reader.read_points()
        units_raw = reader.read_units()

    try:
        ads_raw = system_raw.get("ads")
        ads = ADSSystemRaw(**ads_raw) if isinstance(ads_raw, dict) else None
        runtime_raw = system_raw.get("runtime") or {}
        if not isinstance(runtime_raw, dict):
            raise ConfigError("system.yaml 'runtime' must be a mapping")
        connect_timeout = float(runtime_raw.get("connect_timeout", 10.0))
        write_timeout = float(runtime_raw.get("write_timeout", 5.0))

        models_file = device_config.models if typed else DeviceModelsFile(**models_raw)
        instances_file = device_config.instances if typed else DeviceInstancesFile(**devices_raw)
        tables_file = PointTablesFile(**points_raw) if not typed else None
        units_file = unit_config.definition if typed else UnitsFile(**units_raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid commander configuration: {exc}") from exc

    if typed_reader is not None:
        tables = dict(point_config.tables)
    else:
        assert tables_file is not None
        tables = resolve_point_tables(tables_file.point_tables)
    snapshot, point_meta, disabled = build_core_snapshot(
        models_file=models_file,
        instances_file=instances_file,
        tables=tables,
        units_file=units_file,
    )

    if connect_timeout <= 0:
        raise ConfigError("runtime.connect_timeout must be > 0")
    if write_timeout <= 0:
        raise ConfigError("runtime.write_timeout must be > 0")

    return CommanderConfig(
        core=snapshot,
        ads_local=(
            ADSLocalIdentity(
                local_ams_net_id=ads.local_ams_net_id,
                local_ip=ads.local_ip,
            )
            if ads is not None
            else None
        ),
        connect_timeout=connect_timeout,
        write_timeout=write_timeout,
        point_meta=point_meta,
        disabled_devices=disabled,
    )


__all__ = ["load_commander_config"]
