"""Modbus TCP 连接参数解析。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint, ProtocolOptions

_VALID_MODES = frozenset({"tcp"})
_VALID_WORD_ORDERS = frozenset({"big_endian", "little_endian"})
_ALLOWED_OPTIONS = frozenset({"mode", "slave_id", "timeout", "word_order", "health_point_id", "read_merge_max_gap", "read_plan_cache_size"})


@dataclass(frozen=True, slots=True)
class ModbusConfig:
    """单个 Modbus ConnectionEndpoint 的解析后配置。"""

    host: str
    port: int = 502
    slave_id: int = 1
    mode: str = "tcp"
    timeout: float = 5.0
    word_order: str = "little_endian"
    health_point_id: str | None = None
    read_merge_max_gap: int = 8
    read_plan_cache_size: int = 32


def parse_modbus_config(
    endpoint: ConnectionEndpoint,
    options: ProtocolOptions,
) -> ModbusConfig:
    """从 ConnectionEndpoint 解析 Modbus 参数。"""
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(
            f"connection '{endpoint}' has unknown Modbus options: " f"{sorted(unknown)}"
        )

    mode = _string_option(options.get("mode"), "mode", default="tcp").lower()
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"connection '{endpoint}': unsupported Modbus mode "
            f"'{mode}'; only TCP is currently supported"
        )

    slave_id = _int_option(options.get("slave_id"), "slave_id", default=1)
    if not 0 <= slave_id <= 255:
        raise ConfigError(f"connection '{endpoint}': slave_id must be in 0..255")

    timeout = _float_option(options.get("timeout"), "timeout", default=5.0)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError(f"connection '{endpoint}': timeout must be > 0")

    word_order = _string_option(
        options.get("word_order"),
        "word_order",
        default="little_endian",
    ).lower()
    if word_order not in _VALID_WORD_ORDERS:
        raise ConfigError(f"connection '{endpoint}': invalid word_order " f"'{word_order}'")

    health_point_id = options.get("health_point_id")
    if health_point_id is not None:
        health_point_id = _string_option(health_point_id, "health_point_id", default="")

    read_merge_max_gap = _int_option(
        options.get("read_merge_max_gap"), "read_merge_max_gap", default=8
    )
    read_plan_cache_size = _int_option(
        options.get("read_plan_cache_size"), "read_plan_cache_size", default=32
    )
    if read_merge_max_gap < 0 or read_plan_cache_size < 0:
        raise ConfigError("Modbus read_merge_max_gap and read_plan_cache_size must be >= 0")

    return ModbusConfig(
        host=endpoint.host,
        port=endpoint.port or 502,
        slave_id=slave_id,
        mode=mode,
        timeout=timeout,
        word_order=word_order,
        health_point_id=health_point_id,
        read_merge_max_gap=read_merge_max_gap,
        read_plan_cache_size=read_plan_cache_size,
    )


def _string_option(value: object, name: str, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Modbus option '{name}' must be a non-empty string")
    return value.strip()


def _int_option(value: object, name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Modbus option '{name}' must be an integer")
    return value


def _float_option(value: object, name: str, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"Modbus option '{name}' must be numeric")
    return float(value)
