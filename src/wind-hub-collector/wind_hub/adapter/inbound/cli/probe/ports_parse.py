"""probe ports 的端口规格解析——纯逻辑、无 I/O，可独立单测。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wind_hub.domain.model.errors import ConfigError

if TYPE_CHECKING:
    from wind_hub.config.ports_config import PortsConfig

# 解析约束（决策 3 配套）：单段范围长度上限（防 ``1-65535`` 一次展开六
# 万多个探测任务）与总端口数上限。
_MAX_RANGE_LEN = 1024
_MAX_TOTAL_PORTS = 4096


def default_ports(config: PortsConfig | None = None) -> list[int]:
    """返回默认端口集：端口配置 ``mapping`` 的全部端口。

    ``config`` 为 ``None`` 时用内置工业协议映射（配置文件缺失的兜底）。
    """
    if config is None:
        from wind_hub.config.ports_config import default_ports_config

        config = default_ports_config()
    return list(config.mapping)


def parse_ports(spec: str) -> list[int]:
    """解析端口规格为端口号列表（决策 3）。

    支持逗号分隔的列表（``502,2404,48898``）、起止范围
    （``8000-8100``，闭区间）与两者混合（``502,2404,8000-8100``）。
    重复端口按首次出现位置去重。

    约束：端口须在 1..65535；单段范围长度不超过 1024；去重前总端口
    数不超过 4096。

    Raises:
        ConfigError: 格式非法或超出约束。
    """
    ports: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ConfigError(f"empty port segment in {spec!r}")
        if "-" in part:
            ports.extend(_parse_range(part))
        else:
            ports.append(_parse_single(part, spec))
    if len(ports) > _MAX_TOTAL_PORTS:
        raise ConfigError(
            f"port spec expands to {len(ports)} ports, exceeding the limit "
            f"of {_MAX_TOTAL_PORTS}"
        )
    # 保序去重
    return list(dict.fromkeys(ports))


def _parse_single(part: str, spec: str) -> int:
    """解析单个端口号并校验取值范围。"""
    try:
        port = int(part)
    except ValueError:
        raise ConfigError(f"invalid port {part!r} in {spec!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"port {port} out of range [1, 65535]")
    return port


def _parse_range(part: str) -> list[int]:
    """解析 ``start-end`` 端口范围（闭区间）并校验长度上限。"""
    start_s, _, end_s = part.partition("-")
    try:
        start, end = int(start_s), int(end_s)
    except ValueError:
        raise ConfigError(f"invalid port range {part!r}") from None
    if not 1 <= start <= end <= 65535:
        raise ConfigError(f"port range {part!r} out of bounds; expected 1 <= start <= end <= 65535")
    if end - start + 1 > _MAX_RANGE_LEN:
        raise ConfigError(
            f"port range {part!r} spans {end - start + 1} ports, exceeding "
            f"the per-range limit of {_MAX_RANGE_LEN}"
        )
    return list(range(start, end + 1))
