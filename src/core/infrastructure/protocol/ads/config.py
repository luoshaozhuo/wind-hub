"""ADS 单连接参数解析。"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint, ProtocolOptions

_ALLOWED_OPTIONS = frozenset(
    {
        "target_net_id",
        "timeout",
        "twincat_version",
        "max_subs_per_sum",
        "max_block_bytes",
        "max_gap_bytes",
        "max_delay",
        "max_notifications_per_connection",
        "route_auto_add",
        "username",
        "password",
    }
)


@dataclass(frozen=True, slots=True)
class ADSConfig:
    """单个 ADS ConnectionEndpoint 的解析后参数。"""

    host: str
    target_net_id: str
    target_port: int
    timeout: float = 5.0
    twincat_version: str = "2"
    max_subs_per_sum: int = 500
    max_block_bytes: int = 1024
    max_gap_bytes: int = 16
    max_delay: float = 0.06
    max_notifications_per_connection: int = 550
    route_auto_add: bool = False
    # 远程 PLC 路由注册凭据：wind-hub 应用层默认值，非 pyads 函数默认值。
    # 空密码只表示未提供有效密码，不保证远程 PLC 接受；
    # route_auto_add=false 时不使用凭据。
    username: str = "Administrator"
    password: str = field(default="", repr=False)


def parse_ads_config(
    endpoint: ConnectionEndpoint,
    options: ProtocolOptions,
) -> ADSConfig:
    """从 ConnectionEndpoint 解析并严格校验 ADS 参数。"""
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(f"connection '{endpoint}' has unknown ADS options: " f"{sorted(unknown)}")

    target_net_id = _string_option(
        options.get("target_net_id"),
        "target_net_id",
        default="",
        allow_empty=True,
    )
    if target_net_id and not is_valid_ams_net_id(target_net_id):
        raise ConfigError(f"connection '{endpoint}': invalid target_net_id " f"'{target_net_id}'")

    twincat_version = _string_option(
        options.get("twincat_version"),
        "twincat_version",
        default="2",
    )
    if twincat_version not in {"2", "3"}:
        raise ConfigError(f"connection '{endpoint}': twincat_version must be " "'2' or '3'")

    timeout = _float_option(options.get("timeout"), "timeout", default=5.0)
    if timeout <= 0:
        raise ConfigError(f"connection '{endpoint}': timeout must be > 0")

    max_subs = _int_option(
        options.get("max_subs_per_sum"),
        "max_subs_per_sum",
        default=500,
    )
    if max_subs <= 0:
        raise ConfigError(f"connection '{endpoint}': max_subs_per_sum must be > 0")

    max_block_bytes = _int_option(
        options.get("max_block_bytes"), "max_block_bytes", default=1024
    )
    if max_block_bytes <= 0:
        raise ConfigError("ADS max_block_bytes must be > 0")

    max_gap_bytes = _int_option(
        options.get("max_gap_bytes"), "max_gap_bytes", default=16
    )
    if max_gap_bytes < 0:
        raise ConfigError("ADS max_gap_bytes must be >= 0")

    max_delay = _float_option(
        options.get("max_delay"),
        "max_delay",
        default=0.06,
    )
    if max_delay < 0:
        raise ConfigError(f"connection '{endpoint}': max_delay must be >= 0")

    max_notifications = _int_option(
        options.get("max_notifications_per_connection"),
        "max_notifications_per_connection",
        default=550,
    )
    if max_notifications <= 0:
        raise ConfigError(
            f"connection '{endpoint}': " "max_notifications_per_connection must be > 0"
        )

    route_auto_add = options.get("route_auto_add", False)
    if type(route_auto_add) is not bool:
        raise ConfigError("ADS route_auto_add must be a boolean")

    username = _string_option(options.get("username"), "username", default="Administrator")
    # 密码允许为空（默认未提供有效密码），不套用非空字符串校验。
    password = _string_option(
        options.get("password"),
        "password",
        default="",
        allow_empty=True,
    )
    # AMS Runtime Port：TwinCAT 2 第一 PLC Runtime 为 801，
    # TwinCAT 3 第一 PLC Runtime 为 851（非 ADS 路由器 TCP 端口）。
    default_port = 801 if twincat_version == "2" else 851
    target_port = endpoint.port or default_port

    return ADSConfig(
        host=endpoint.host,
        target_net_id=target_net_id,
        target_port=target_port,
        timeout=timeout,
        twincat_version=twincat_version,
        max_subs_per_sum=max_subs,
        max_block_bytes=max_block_bytes,
        max_gap_bytes=max_gap_bytes,
        max_delay=max_delay,
        max_notifications_per_connection=max_notifications,
        route_auto_add=route_auto_add,
        username=username,
        password=password,
    )


def is_valid_ams_net_id(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 6 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _string_option(
    value: object,
    name: str,
    *,
    default: str,
    allow_empty: bool = False,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"ADS option '{name}' must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ConfigError(f"ADS option '{name}' must not be empty")
    return normalized


def _int_option(value: object, name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"ADS option '{name}' must be an integer")
    return value


def _float_option(value: object, name: str, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"ADS option '{name}' must be numeric")
    return float(value)
