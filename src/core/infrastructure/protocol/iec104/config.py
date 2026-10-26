"""IEC 60870-5-104 单 TCP 连接参数解析（多 Common Address）。

一条 TCP 连接可承载多个 CA；``common_addresses`` 允许配置为单个整数或
逗号分隔整数串（协议选项只支持标量值）。点定义可用 ``ca`` 显式指定所属
CA，缺省归属配置中的第一个 CA。
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint, ProtocolOptions

_ALLOWED_OPTIONS = frozenset(
    {"common_addresses", "health_point", "timezone", "k", "w", "t0", "t1", "t2", "t3"}
)
_MAX_CA = 65535


@dataclass(frozen=True, slots=True)
class IEC104Config:
    """单个 IEC104 ConnectionEndpoint 的解析后参数。

    Attributes:
        common_addresses: 本连接管理的全部 CA（按配置顺序，首项为点缺省 CA）。
        health_point: 应用层健康检查点的 point_id；None 表示只做链路健康判断。
        t0: TCP 建连超时（s）。
        t1: 应用层响应/命令确认超时（s，hat response_timeout）。
        t2: 监控帧确认超时（s，hat supervisory_timeout）。
        t3: TESTFR keep-alive 间隔（s，hat test_timeout）。
        k/w: IEC104 发送/接收窗口。
    """

    host: str
    port: int
    common_addresses: tuple[int, ...] = (1,)
    health_point: str | None = None
    timezone: str | None = None
    k: int = 12
    w: int = 8
    t0: float = 30.0
    t1: float = 15.0
    t2: float = 10.0
    t3: float = 20.0

    @property
    def default_ca(self) -> int:
        """点定义未显式指定 ``ca`` 时归属的 CA。"""
        return self.common_addresses[0]


def parse_iec104_config(
    endpoint: ConnectionEndpoint,
    options: ProtocolOptions,
) -> IEC104Config:
    """从 ConnectionEndpoint 与协议选项解析 IEC104 连接参数。"""
    unknown = set(options) - _ALLOWED_OPTIONS
    if unknown:
        hint = ""
        if "common_addr" in unknown:
            hint = (
                " (legacy 'common_addr' was replaced by 'common_addresses': "
                "int or comma-separated string)"
            )
        raise ConfigError(
            f"connection '{endpoint}' has unknown IEC104 options: "
            f"{sorted(unknown)}{hint}"
        )

    common_addresses = _parse_cas(options.get("common_addresses"), endpoint)
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

    t0 = _positive_float(options.get("t0"), "t0", default=30.0)
    t1 = _positive_float(options.get("t1"), "t1", default=15.0)
    t2 = _positive_float(options.get("t2"), "t2", default=10.0)
    t3 = _positive_float(options.get("t3"), "t3", default=20.0)
    if t2 > t1:
        raise ConfigError(f"connection '{endpoint}': IEC104 t2 ({t2}) must not exceed t1 ({t1})")

    return IEC104Config(
        host=endpoint.host,
        port=endpoint.port or 2404,
        common_addresses=common_addresses,
        health_point=health_point,
        timezone=timezone,
        k=k,
        w=w,
        t0=t0,
        t1=t1,
        t2=t2,
        t3=t3,
    )


def _parse_cas(value: object, endpoint: ConnectionEndpoint) -> tuple[int, ...]:
    """解析 ``common_addresses``：整数或逗号分隔整数串，去重并保持顺序。"""
    if value is None:
        return (1,)
    if isinstance(value, bool):
        raise ConfigError("IEC104 option 'common_addresses' must be int or comma-separated ints")
    raw_items: list[str | int]
    if isinstance(value, int):
        raw_items = [value]
    elif isinstance(value, str) and value.strip():
        raw_items = [part.strip() for part in value.split(",")]
    else:
        raise ConfigError("IEC104 option 'common_addresses' must be int or comma-separated ints")

    cas: list[int] = []
    for item in raw_items:
        try:
            ca = int(item)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"IEC104 common address '{item}' must be an integer"
            ) from exc
        if not 0 <= ca <= _MAX_CA:
            raise ConfigError(f"connection '{endpoint}': common address {ca} out of 0..65535")
        if ca not in cas:
            cas.append(ca)
    if not cas:
        raise ConfigError("IEC104 option 'common_addresses' must not be empty")
    return tuple(cas)


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
