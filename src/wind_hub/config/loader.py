"""Configuration loader — YAML reading, per-model validation, cross-file checks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from wind_hub.config.reporting import load_reporting
from wind_hub.config.schema import (
    Config,
    DevicesConfig,
    PointsConfig,
    ReportingConfig,
    RoutingConfig,
    SystemConfig,
)
from wind_hub.domain.model.errors import ConfigError

if TYPE_CHECKING:
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a single YAML file.

    Raises:
        ConfigError: If the file is missing or contains invalid YAML.
    """
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = cast(dict[str, Any], yaml.safe_load(fh))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"Empty configuration file: {path}")
    return data


def load_system(path: Path) -> SystemConfig:
    """Load and validate ``system.yaml``."""
    raw = _read_yaml(path)
    try:
        return SystemConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid system config [{path}]: {exc}") from exc


def load_devices(path: Path) -> DevicesConfig:
    """Load and validate ``devices.yaml``."""
    raw = _read_yaml(path)
    try:
        return DevicesConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid devices config [{path}]: {exc}") from exc


def load_points(path: Path) -> PointsConfig:
    """Load and validate ``points.yaml``."""
    raw = _read_yaml(path)
    try:
        return PointsConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid points config [{path}]: {exc}") from exc


def load_routing(path: Path) -> RoutingConfig:
    """Load and validate ``routing.yaml``."""
    raw = _read_yaml(path)
    try:
        return RoutingConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid routing config [{path}]: {exc}") from exc


def load_config(config_dir: str | Path) -> Config:
    """Load all four configuration files from a directory, validate each,
    run cross-file consistency checks, and return an aggregate ``Config``.

    Expected files:
        ``system.yaml``, ``devices.yaml``, ``points.yaml``, ``routing.yaml``

    Cross-file checks:
        - Every point ``device_id`` exists in devices.
        - Per-point ``sinks`` names exist in system sinks.
        - Routing rule ``targets`` names exist in system sinks.

    Raises:
        ConfigError: On any validation or consistency failure.
    """
    base = Path(config_dir)

    system = load_system(base / "system.yaml")
    devices = load_devices(base / "devices.yaml")
    points = load_points(base / "points.yaml")
    routing = load_routing(base / "routing.yaml")

    # Optional IEC104 slave proxy config — absent means no proxy.
    reporting: ReportingConfig | None = None
    reporting_path = base / "reporting.yaml"
    if reporting_path.is_file():
        reporting = load_reporting(reporting_path)

    # Build lookup sets for cross-file validation
    device_ids = {d.device_id for d in devices.devices}
    sink_names = {s.name for s in system.sinks}

    # Cross-file: every point's device_id must exist
    for p in points.points:
        if p.device_id not in device_ids:
            raise ConfigError(
                f"Point '{p.device_id}/{p.point_id}' references " f"unknown device '{p.device_id}'"
            )

    # Cross-file: per-point sinks must reference valid sink names
    for p in points.points:
        if p.sinks is None:
            continue
        for sn in p.sinks:
            if sn not in sink_names:
                raise ConfigError(
                    f"Point '{p.device_id}/{p.point_id}' references "
                    f"unknown sink '{sn}' (available: {sorted(sink_names)})"
                )

    # Cross-file: routing rule targets must reference valid sink names
    for rule in routing.rules:
        for sn in rule.targets:
            if sn not in sink_names:
                raise ConfigError(
                    f"Routing rule '{rule.name}' targets unknown sink "
                    f"'{sn}' (available: {sorted(sink_names)})"
                )

    return Config(
        system=system,
        devices=devices,
        points=points,
        routing=routing,
        reporting=reporting,
    )
