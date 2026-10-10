"""YAML -> Core Domain 的直接解析。无 Config VO 中间模型。"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.settings import ADSLocalConfig, RuntimeSettings, SystemSettings
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    DataType,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    Site,
    Sink,
    Task,
    validate_core_config,
)
from core.domain.unit import UNIT_CATALOG, UnitCode
from core.infrastructure.protocol.ads.config import is_valid_ams_net_id, parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config
from .business_points import parse_business_points
from .snapshot_writer import _yaml_plain
from .yaml import read_yaml_mapping


_OPTION_PARSERS = {
    "ads": parse_ads_config,
    "modbus": parse_modbus_config,
    "iec104": parse_iec104_config,
}
_POINT_FIELDS = {
    "point_id", "business_point_id", "variable_name", "point_groups",
    "address", "data_type", "scale", "offset", "unit", "description",
}


def _map(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{context} must be a mapping with string keys")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{context} must be a list")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ConfigError(f"{context} unknown keys: {sorted(extra)}")


def _name(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value.strip()


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ConfigError(f"{context} must be a finite number")
    return float(value)


def _flag(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be boolean")
    return value


def _system(raw: Mapping[str, Any]) -> tuple[str, str, SystemSettings]:
    site = _map(raw.get("site"), "system.site")
    _keys(site, {"site_id", "name"}, "system.site")
    site_id = _name(site.get("site_id"), "system.site_id")
    name = _name(site.get("name", site_id), "system.site.name")
    runtime_raw = _map(raw.get("runtime", {}), "system.runtime")
    _keys(runtime_raw, set(RuntimeSettings.__dataclass_fields__), "system.runtime")
    settings = dict(runtime_raw)
    for key in ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout", "retry_interval"):
        if key in settings and settings[key] is not None:
            settings[key] = _number(settings[key], f"runtime.{key}")
    if "queue_maxsize" in settings and settings["queue_maxsize"] is not None:
        value = settings["queue_maxsize"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("runtime.queue_maxsize must be an integer")
    if "read_retries" in settings:
        value = settings["read_retries"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("runtime.read_retries must be an integer")
    runtime = RuntimeSettings(**settings)
    ads_raw = raw.get("ads")
    ads = None
    if ads_raw is not None:
        values = _map(ads_raw, "system.ads")
        _keys(values, set(ADSLocalConfig.__dataclass_fields__), "system.ads")
        net_id = _name(values.get("local_ams_net_id"), "ads.local_ams_net_id")
        if not is_valid_ams_net_id(net_id):
            raise ConfigError(f"invalid AMS Net ID '{net_id}'")
        ads = ADSLocalConfig(
            local_ams_net_id=net_id,
            local_ip=_name(values.get("local_ip"), "ads.local_ip"),
            username=_name(values.get("username", "Administrator"), "ads.username"),
            password=values.get("password", ""),
        )
    return site_id, name, SystemSettings(runtime=runtime, ads=ads)
