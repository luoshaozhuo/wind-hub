"""IEC104 slave proxy reporting config loader.

Loads the optional ``reporting.yaml`` file: the mapping between the
engine's ``(device_id, point_id)`` identities and IEC104 information-object
addresses (IOA) plus the monitor-direction ASDU type used to report each
point to a dispatch master.

The report config is optional — a deployment without ``reporting.yaml``
simply has no slave proxy (``Config.reporting`` is ``None``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub.config.schema import ReportingConfig
from wind_hub.domain.model.errors import ConfigError


def load_reporting(path: str | Path) -> ReportingConfig:
    """Load and validate ``reporting.yaml``.

    Raises:
        ConfigError: If the file is missing, contains invalid YAML, or
            fails :class:`ReportingConfig` validation.
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
