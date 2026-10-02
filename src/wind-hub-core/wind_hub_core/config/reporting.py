"""可选 IEC104 slave proxy reporting.yaml 加载器。

reporting 配置把 Collector 的 (device_id, point_id) 映射为调度侧 IOA 和监视方向
TypeID。文件不存在时由上层视为“不启用 slave proxy”；本模块只负责存在文件的
安全 YAML 解析和 ReportingConfig schema 校验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub_core.config.schema import ReportingConfig
from wind_hub_core.model.errors import ConfigError


def load_reporting(path: str | Path) -> ReportingConfig:
    """加载并校验 reporting.yaml。

    Args:
        path: reporting.yaml 路径。

    Returns:
        ReportingConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Reporting config file not found: {p}")
    try:
        with open(p, encoding="utf-8") as fh:
            raw = cast(dict[str, Any], yaml.safe_load(fh))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"Empty configuration file: {p}")
    try:
        return ReportingConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid reporting config [{p}]: {exc}") from exc
