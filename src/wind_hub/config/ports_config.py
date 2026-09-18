"""端口扫描策略配置加载（step24）。

``wind-hub probe ports`` 的默认端口集与「端口 → 服务名」映射从
``configs/ports.yaml`` 读取；文件缺失时回落到内置默认值（向后兼容
step20 的硬编码行为），YAML 非法或校验失败抛 :class:`ConfigError`。

纯加载 + 校验逻辑，无 I/O 副作用（不引入新依赖，只用已依赖的 PyYAML）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub.domain.model.errors import ConfigError

# CLI 未显式给 --ports-config 时的默认查找路径（相对当前工作目录，
# 与 system.yaml 等配置同一部署约定）。
DEFAULT_PORTS_CONFIG_PATH = Path("configs/ports.yaml")

# 内置默认端口集 / 服务映射：与 step20 硬编码值一致（向后兼容兜底）。
_BUILTIN_DEFAULT_PORTS: tuple[int, ...] = (502, 2404, 48898, 4840, 44818)
_BUILTIN_SERVICE_MAP: dict[int, str] = {
    502: "modbus",
    2404: "iec104",
    48898: "ads",
    4840: "opc-ua",
    44818: "ethernet-ip",
    80: "http",
    443: "https",
    22: "ssh",
    23: "telnet",
}


@dataclass(frozen=True)
class PortsConfig:
    """端口扫描配置。"""

    default_ports: list[int]
    """未指定 ``--ports`` 时扫描的端口集。"""
    service_map: dict[int, str] = field(default_factory=dict)
    """端口 → 服务名（报告标注用）。"""
    default_timeout: float = 1.0
    """单端口默认超时（秒）。"""
    default_concurrency: int = 128
    """默认并发探测数。"""


def default_ports_config() -> PortsConfig:
    """返回内置默认配置（与 step20 硬编码行为一致，向后兼容）。"""
    return PortsConfig(
        default_ports=list(_BUILTIN_DEFAULT_PORTS),
        service_map=dict(_BUILTIN_SERVICE_MAP),
        default_timeout=1.0,
        default_concurrency=128,
    )


def load_ports_config(path: str | Path | None = None) -> PortsConfig:
    """加载端口扫描配置。

    - ``path`` 为 ``None`` 时用 :data:`DEFAULT_PORTS_CONFIG_PATH`；
    - 文件不存在时返回内置默认值（向后兼容：没有配置文件的部署保持
      step20 的扫描行为）；
    - YAML 解析失败或校验不通过抛 :class:`ConfigError`。

    校验：端口须在 1..65535 且无重复；``service_map`` 键为合法端口、
    值为非空字符串；超时 > 0；并发 >= 1。
    """
    p = Path(path) if path is not None else DEFAULT_PORTS_CONFIG_PATH
    if not p.is_file():
        return default_ports_config()
    try:
        with open(p, encoding="utf-8") as fh:
            raw = cast(dict[str, Any], yaml.safe_load(fh))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"Empty configuration file: {p}")
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid ports config [{p}]: top level must be a mapping")
    return _parse(raw, p)


def _parse(raw: dict[str, Any], path: Path) -> PortsConfig:
    defaults = default_ports_config()

    default_ports = raw.get("default_ports", defaults.default_ports)
    if not isinstance(default_ports, list) or not default_ports:
        raise ConfigError(f"Invalid ports config [{path}]: default_ports must be a non-empty list")
    _validate_ports(default_ports, "default_ports", path)

    service_map = raw.get("service_map", defaults.service_map)
    if not isinstance(service_map, dict):
        raise ConfigError(f"Invalid ports config [{path}]: service_map must be a mapping")
    _validate_ports(list(service_map), "service_map keys", path)
    for port, name in service_map.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f"Invalid ports config [{path}]: service_map[{port}] must be a non-empty string"
            )

    timeout = raw.get("default_timeout", defaults.default_timeout)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"Invalid ports config [{path}]: default_timeout must be > 0")

    concurrency = raw.get("default_concurrency", defaults.default_concurrency)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ConfigError(f"Invalid ports config [{path}]: default_concurrency must be >= 1")

    return PortsConfig(
        default_ports=[int(p) for p in default_ports],
        service_map={int(k): str(v) for k, v in service_map.items()},
        default_timeout=float(timeout),
        default_concurrency=int(concurrency),
    )


def _validate_ports(ports: list[Any], what: str, path: Path) -> None:
    """校验端口列表：全为 int、范围 1..65535、无重复。"""
    seen: set[int] = set()
    for port in ports:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(
                f"Invalid ports config [{path}]: {what} contains invalid port {port!r}; "
                "expected int in [1, 65535]"
            )
        if port in seen:
            raise ConfigError(
                f"Invalid ports config [{path}]: {what} contains duplicate port {port}"
            )
        seen.add(port)
