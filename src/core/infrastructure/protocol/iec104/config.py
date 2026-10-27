"""IEC 60870-5-104 单 TCP 连接参数解析（多 Common Address）。

每个测点在点表中显式指定 CA；同一 TCP 连接可以服务多个 CA。
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint, ProtocolOptions

_ALLOWED_OPTIONS = frozenset(
    {"health_point", "timezone", "k", "w", "t0", "t1", "t2", "t3", "read_timeout", "command_timeout", "interrogation_timeout"}
)


@dataclass(frozen=True, slots=True)
class IEC104Config:
    """单个 IEC104 ConnectionEndpoint 的解析后参数。

    Attributes:
        health_point: 应用层健康检查点的 point_id；None 表示自动选择可读测点。
        t0: TCP 建连超时（s）。
        t1: IEC104 传输层 I 帧确认超时（s，hat response_timeout）。
        t2: 监控帧确认超时（s，hat supervisory_timeout）。
        t3: TESTFR keep-alive 间隔（s，hat test_timeout）。
        k/w: IEC104 发送/接收窗口。
        read_timeout: C_RD_NA_1 业务响应超时（s）。
        command_timeout: 每个控制阶段的业务确认超时（s）。
        interrogation_timeout: 每个 CA 召唤的业务完成超时（s）。
    """

    host: str
    port: int
    health_point: str | None = None
    timezone: str | None = None
    k: int = 12
    w: int = 8
    t0: float = 5.0
    t1: float = 5.0
    t2: float = 1.0
    t3: float = 10.0
    read_timeout: float = 2.0
    command_timeout: float = 3.0
    interrogation_timeout: float = 5.0


def parse_iec104_config(
    endpoint: ConnectionEndpoint,
    options: ProtocolOptions,
) -> IEC104Config:
    """从 ConnectionEndpoint 与协议选项解析 IEC104 连接参数。"""
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        raise ConfigError(f"connection '{endpoint}' has unknown IEC104 options: {sorted(unknown)}")

    health_point = _optional_str(options.get("health_point"), "health_point")
    timezone = _optional_str(options.get("timezone"), "timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigError(f"IEC104 invalid timezone: {timezone}") from exc

    k = _positive_int(options.get("k"), "k", default=12)
    w = _positive_int(options.get("w"), "w", default=8)
    if w > k:
        raise ConfigError(f"connection '{endpoint}': IEC104 w ({w}) must not exceed k ({k})")

    t0 = _positive_float(options.get("t0"), "t0", default=5.0)
    t1 = _positive_float(options.get("t1"), "t1", default=5.0)
    t2 = _positive_float(options.get("t2"), "t2", default=1.0)
    t3 = _positive_float(options.get("t3"), "t3", default=10.0)
    read_timeout = _positive_float(options.get("read_timeout"), "read_timeout", default=2.0)
    command_timeout = _positive_float(options.get("command_timeout"), "command_timeout", default=3.0)
    interrogation_timeout = _positive_float(
        options.get("interrogation_timeout"), "interrogation_timeout", default=5.0
    )
    if t2 > t1:
        raise ConfigError(f"connection '{endpoint}': IEC104 t2 ({t2}) must not exceed t1 ({t1})")

    return IEC104Config(
        host=endpoint.host,
        port=endpoint.port or 2404,
        health_point=health_point,
        timezone=timezone,
        k=k,
        w=w,
        t0=t0,
        t1=t1,
        t2=t2,
        t3=t3,
        read_timeout=read_timeout,
        command_timeout=command_timeout,
        interrogation_timeout=interrogation_timeout,
    )


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"IEC104 option '{name}' must be a non-empty string")
    return value.strip()


def _positive_int(value: object, name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"IEC104 option '{name}' must be an integer")
    if value < 1:
        raise ConfigError(f"IEC104 option '{name}' must be >= 1")
    return value


def _positive_float(value: object, name: str, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"IEC104 option '{name}' must be numeric")
    result = float(value)
    if result <= 0:
        raise ConfigError(f"IEC104 option '{name}' must be > 0")
    return result
