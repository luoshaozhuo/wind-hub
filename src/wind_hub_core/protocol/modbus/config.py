"""Modbus 连接参数模型与 DeviceConfig 转换。

当前只实现 Modbus TCP。RTU 作为合法配置值保留用于显式报错，但不会拉入
pyserial 依赖。该模块只负责参数解析和校验，不建立 socket。
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub_core.config import DeviceConfig
from wind_hub_core.model.errors import ConfigError


@dataclass(frozen=True)
class ModbusConfig:
    """单设备 Modbus 连接参数。

    所有字段不可变；from_device_config 是统一构造入口并负责校验。
    """

    host: str
    """Modbus Server 的主机名或 IP。"""

    port: int = 502
    """TCP 端口，默认 502。"""

    unit_id: int = 1
    """Modbus unit/slave ID，范围 0~255。"""

    mode: str = "tcp"
    """传输模式 tcp 或 rtu；当前仅实现 tcp。"""

    timeout: float = 5.0
    """单次 Modbus transaction 超时，单位秒。"""

    word_order: str = "little_endian"
    """多寄存器数据的默认 word order。

    big_endian：低地址寄存器保存高 16 位；
    little_endian：低地址寄存器保存低 16 位。
    """


_VALID_MODES = frozenset({"tcp", "rtu"})
_VALID_WORD_ORDERS = frozenset({"big_endian", "little_endian"})


def from_device_config(cfg: DeviceConfig) -> ModbusConfig:
    """从 DeviceConfig 构造并校验 ModbusConfig。

    Args:
        cfg: 已通过基础 schema 校验的设备配置。

    Returns:
        解析后的 ModbusConfig。

    Raises:
        ConfigError: mode、unit_id、timeout 或 word_order 非法。
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
        word_order=word_order,
    )
