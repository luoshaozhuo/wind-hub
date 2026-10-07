"""ADS 单连接参数解析。"""

from __future__ import annotations

from dataclasses import dataclass

from core.application import ConfigError, DeviceConnection

_ALLOWED_OPTIONS = frozenset(
    {
        "target_net_id",
        "timeout",
        "twincat_version",
        "read_mode",
        "max_subs_per_sum",
        "max_concurrent_reads",
    }
)
_VALID_READ_MODES = frozenset({"sum", "sequential"})


@dataclass(frozen=True, slots=True)
class ADSConfig:
    """单个 ADS DeviceConnection 的解析后参数。"""

    host: str
    target_net_id: str
    target_port: int
    timeout: float = 5.0
    twincat_version: str = "2"
    read_mode: str = "sum"
    max_subs_per_sum: int = 500
    max_concurrent_reads: int = 16


def parse_ads_config(connection: DeviceConnection) -> ADSConfig:
    """从共享 DeviceConnection 解析并严格校验 ADS 参数。"""
    options = connection.endpoint.options
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(
            f"connection '{connection.connection_id}' has unknown ADS options: "
            f"{sorted(unknown)}"
        )

    target_net_id = _string_option(
        options.get("target_net_id"),
        "target_net_id",
        default="",
        allow_empty=True,
    )
    if target_net_id and not _is_valid_ams_net_id(target_net_id):
        raise ConfigError(
            f"connection '{connection.connection_id}': invalid target_net_id "
            f"'{target_net_id}'"
        )

    twincat_version = _string_option(
        options.get("twincat_version"),
        "twincat_version",
        default="2",
    )
    if twincat_version not in {"2", "3"}:
        raise ConfigError(
            f"connection '{connection.connection_id}': twincat_version must be "
            "'2' or '3'"
        )

    read_mode = _string_option(
        options.get("read_mode"),
        "read_mode",
        default="sum",
    ).lower()
    if read_mode not in _VALID_READ_MODES:
        raise ConfigError(
            f"connection '{connection.connection_id}': invalid ADS read_mode "
            f"'{read_mode}'"
        )

    timeout = _float_option(options.get("timeout"), "timeout", default=5.0)
    if timeout <= 0:
        raise ConfigError(
            f"connection '{connection.connection_id}': timeout must be > 0"
        )

    max_subs = _int_option(
        options.get("max_subs_per_sum"),
        "max_subs_per_sum",
        default=500,
    )
    if max_subs <= 0:
        raise ConfigError(
            f"connection '{connection.connection_id}': max_subs_per_sum must be > 0"
        )

    max_concurrent = _int_option(
        options.get("max_concurrent_reads"),
        "max_concurrent_reads",
        default=16,
    )
    if max_concurrent <= 0:
        raise ConfigError(
            f"connection '{connection.connection_id}': "
            "max_concurrent_reads must be > 0"
        )

    default_port = 801 if twincat_version == "2" else 802
    target_port = connection.endpoint.port or default_port

    return ADSConfig(
        host=connection.endpoint.host,
        target_net_id=target_net_id,
        target_port=target_port,
        timeout=timeout,
        twincat_version=twincat_version,
        read_mode=read_mode,
        max_subs_per_sum=max_subs,
        max_concurrent_reads=max_concurrent,
    )


def _is_valid_ams_net_id(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 6 and all(
        part.isdigit() and 0 <= int(part) <= 255
        for part in parts
    )


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
