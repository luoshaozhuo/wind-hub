"""IEC 60870-5-104 单连接参数解析。"""

from __future__ import annotations

from dataclasses import dataclass

from core.application.config import DeviceConnection
from core.application.errors import ConfigError

_ALLOWED_OPTIONS = frozenset({"common_addr", "k", "w", "t0", "t1", "t2", "t3"})


@dataclass(frozen=True, slots=True)
class IEC104Config:
    """单个 IEC104 DeviceConnection 的解析后参数。"""

    host: str
    port: int
    common_addr: int = 1
    k: int = 12
    w: int = 8
    t0: float = 30.0
    t1: float = 15.0
    t2: float = 10.0
    t3: float = 20.0


def parse_iec104_config(connection: DeviceConnection) -> IEC104Config:
    """从共享 DeviceConnection 解析 IEC104 参数。"""
    options = connection.endpoint.options
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(
            f"connection '{connection.connection_id}' has unknown IEC104 options: "
            f"{sorted(unknown)}"
        )

    common_addr = _int_option(options.get("common_addr"), "common_addr", default=1)
    if not 0 <= common_addr <= 65535:
        raise ConfigError(
            f"connection '{connection.connection_id}': common_addr must be in 0..65535"
        )

    k = _positive_int(options.get("k"), "k", default=12)
    w = _positive_int(options.get("w"), "w", default=8)
    if w > k:
        raise ConfigError(
            f"connection '{connection.connection_id}': IEC104 w ({w}) must not exceed k ({k})"
        )

    t0 = _positive_float(options.get("t0"), "t0", default=30.0)
    t1 = _positive_float(options.get("t1"), "t1", default=15.0)
    t2 = _positive_float(options.get("t2"), "t2", default=10.0)
    t3 = _positive_float(options.get("t3"), "t3", default=20.0)
    if t2 > t1:
        raise ConfigError(
            f"connection '{connection.connection_id}': IEC104 t2 ({t2}) must not exceed t1 ({t1})"
        )

    return IEC104Config(
        host=connection.endpoint.host,
        port=connection.endpoint.port or 2404,
        common_addr=common_addr,
        k=k,
        w=w,
        t0=t0,
        t1=t1,
        t2=t2,
        t3=t3,
    )


def _int_option(value: object, name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"IEC104 option '{name}' must be an integer")
    return value


def _positive_int(value: object, name: str, *, default: int) -> int:
    result = _int_option(value, name, default=default)
    if result < 1.0:
        raise ConfigError(
            f"IEC104 option '{name}' must be >= 1s for c104"
        )
    return result


def _positive_float(value: object, name: str, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"IEC104 option '{name}' must be numeric")
    result = float(value)
    if result <= 0:
        raise ConfigError(f"IEC104 option '{name}' must be > 0")
    return result
