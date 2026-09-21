"""端口扫描策略配置加载。

``wind-hub probe ports`` 的「端口 → 服务名」映射从 ``configs/ports.yaml``
读取；未指定 ``--ports`` 时扫描 ``mapping`` 的全部端口。文件缺失时回落到
内置工业协议映射；YAML 非法或校验失败抛 :class:`ConfigError`。

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

# 内置「端口 → 服务名」映射：工业协议为主，辅以少量通用管理端口。
# 未加载配置文件时作为兜底（文件缺失的部署保持可扫描）。
_BUILTIN_MAPPING: dict[int, str] = {
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

    mapping: dict[int, str] = field(default_factory=dict)
    """端口 → 服务名；``mapping.keys()`` 即未指定 ``--ports`` 时的扫描端口集。"""
    timeout: float = 1.0
    """单端口默认超时（秒）。"""
    concurrency: int = 128
    """默认并发探测数。"""


def default_ports_config() -> PortsConfig:
    """返回内置默认配置（工业协议映射 + 默认超时/并发）。"""
    return PortsConfig(mapping=dict(_BUILTIN_MAPPING), timeout=1.0, concurrency=128)


def load_ports_config(path: str | Path | None = None) -> PortsConfig:
    """加载端口扫描配置。

    - ``path`` 为 ``None`` 时用 :data:`DEFAULT_PORTS_CONFIG_PATH`；
    - 文件不存在时返回内置默认值；
    - YAML 解析失败或校验不通过抛 :class:`ConfigError`。

    校验：``mapping`` 必须是非空映射，键为合法端口（1..65535）、值为非空
    字符串；``timeout`` > 0；``concurrency`` >= 1。
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

    mapping = raw.get("mapping", defaults.mapping)
    if not isinstance(mapping, dict) or not mapping:
        raise ConfigError(f"Invalid ports config [{path}]: mapping must be a non-empty mapping")
    _validate_ports(list(mapping), "mapping keys", path)
    for port, name in mapping.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f"Invalid ports config [{path}]: mapping[{port}] must be a non-empty string"
            )

    timeout = raw.get("timeout", defaults.timeout)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"Invalid ports config [{path}]: timeout must be > 0")

    concurrency = raw.get("concurrency", defaults.concurrency)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ConfigError(f"Invalid ports config [{path}]: concurrency must be >= 1")

    return PortsConfig(
        mapping={int(k): str(v) for k, v in mapping.items()},
        timeout=float(timeout),
        concurrency=int(concurrency),
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
