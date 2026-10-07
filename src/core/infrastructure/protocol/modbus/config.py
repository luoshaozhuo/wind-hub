"""Modbus TCP 连接参数解析。"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain import ConnectionEndpoint, ProtocolOptions
from core.application.errors import ConfigError

_VALID_MODES = frozenset({"tcp"})
_VALID_WORD_ORDERS = frozenset({"big_endian", "little_endian"})
_ALLOWED_OPTIONS = frozenset({"mode", "unit_id", "timeout", "word_order"})


@dataclass(frozen=True, slots=True)
class ModbusConfig:
    """单个 Modbus ConnectionEndpoint 的解析后配置。"""

    host: str
    port: int = 502
    unit_id: int = 1
    mode: str = "tcp"
    timeout: float = 5.0
    word_order: str = "little_endian"


def parse_modbus_config(
    endpoint: ConnectionEndpoint,
    options: ProtocolOptions,
) -> ModbusConfig:
    """从 ConnectionEndpoint 解析 Modbus 参数。"""
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(
            f"connection '{endpoint}' has unknown Modbus options: "
            f"{sorted(unknown)}"
        )

    mode = _string_option(options.get("mode"), "mode", default="tcp").lower()
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"connection '{endpoint}': unsupported Modbus mode "
            f"'{mode}'; only TCP is currently supported"
        )

    unit_id = _int_option(options.get("unit_id"), "unit_id", default=1)
    if not 0 <= unit_id <= 255:
        raise ConfigError(
            f"connection '{endpoint}': unit_id must be in 0..255"
        )

    timeout = _float_option(options.get("timeout"), "timeout", default=5.0)
    if timeout <= 0:
        raise ConfigError(
            f"connection '{endpoint}': timeout must be > 0"
        )

    word_order = _string_option(
        options.get("word_order"),
        "word_order",
        default="little_endian",
    ).lower()
    if word_order not in _VALID_WORD_ORDERS:
        raise ConfigError(
            f"connection '{endpoint}': invalid word_order "
            f"'{word_order}'"
        )

    return ModbusConfig(
        host=endpoint.host,
        port=endpoint.port or 502,
        unit_id=unit_id,
        mode=mode,
        timeout=timeout,
        word_order=word_order,
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
