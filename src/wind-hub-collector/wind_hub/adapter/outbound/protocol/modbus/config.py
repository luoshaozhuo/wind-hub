"""Modbus connection parameters.

``ModbusConfig`` is derived from a :class:`~wind_hub.config.schema.DeviceConfig`
by :func:`from_device_config`.  Only TCP transport is supported; RTU is
recognised for validation but the driver raises ``NotImplementedError`` when
asked to use it (no ``pyserial`` dependency is pulled in).
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.errors import ConfigError


@dataclass(frozen=True)
class ModbusConfig:
    """Modbus connection parameters.

    All fields are immutable; :func:`from_device_config` is the single
    construction point and performs validation.
    """

    host: str
    """Hostname or IP address of the Modbus server."""

    port: int = 502
    """TCP port (default Modbus TCP port 502)."""

    unit_id: int = 1
    """Modbus unit/slave identifier (0-255)."""

    mode: str = "tcp"
    """Transport mode: ``"tcp"`` or ``"rtu"`` (only ``"tcp"`` implemented)."""

    timeout: float = 5.0
    """Transaction timeout in seconds."""

    reconnect_max_retries: int = 5
    """Maximum consecutive reconnect attempts before entering FAILED state."""

    reconnect_backoff_max: float = 30.0
    """Upper bound (seconds) for exponential reconnect backoff."""

    word_order: str = "little_endian"
    """Default multi-register word order: ``"big_endian"`` or ``"little_endian"``.

    ``big_endian``: 低地址寄存器 = 高 16 位；
    ``little_endian``: 低地址寄存器 = 低 16 位。"""


_VALID_MODES = frozenset({"tcp", "rtu"})
_VALID_WORD_ORDERS = frozenset({"big_endian", "little_endian"})


def from_device_config(cfg: DeviceConfig) -> ModbusConfig:
    """Build and validate a :class:`ModbusConfig` from *cfg*.

    Raises:
        ConfigError: On an invalid ``mode``, out-of-range ``unit_id``,
            non-positive ``timeout``, or invalid ``word_order``.
    """
    ext = cfg.endpoint.extensions

    mode = str(ext.get("mode", "tcp"))
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"Modbus device '{cfg.device_id}': invalid mode '{mode}'; "
            f"must be one of {sorted(_VALID_MODES)}"
        )

    unit_id = int(ext.get("unit_id", 1))
    if not 0 <= unit_id <= 255:
        raise ConfigError(f"Modbus device '{cfg.device_id}': unit_id {unit_id} out of range 0-255")

    timeout = float(ext.get("timeout", 5.0))
    if timeout <= 0:
        raise ConfigError(f"Modbus device '{cfg.device_id}': timeout must be > 0, got {timeout}")

    word_order = str(ext.get("word_order", "little_endian"))
    if word_order not in _VALID_WORD_ORDERS:
        raise ConfigError(
            f"Modbus device '{cfg.device_id}': invalid word_order '{word_order}'; "
            f"must be one of {sorted(_VALID_WORD_ORDERS)}"
        )

    return ModbusConfig(
        host=cfg.endpoint.host,
        port=int(ext.get("port", cfg.endpoint.port)),
        unit_id=unit_id,
        mode=mode,
        timeout=timeout,
        reconnect_max_retries=int(ext.get("reconnect_max_retries", 5)),
        reconnect_backoff_max=float(ext.get("reconnect_backoff_max", 30.0)),
        word_order=word_order,
    )
