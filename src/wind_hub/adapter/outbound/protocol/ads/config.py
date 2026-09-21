"""ADS connection parameters.

``ADSConfig`` is derived from a :class:`~wind_hub.config.schema.DeviceConfig`
by :func:`from_device_config`.  The target IP address is taken from
``endpoint.host`` and is not part of this dataclass (it is read directly by the
driver).
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.errors import ConfigError


@dataclass(frozen=True)
class ADSConfig:
    """ADS (Automation Device Specification) connection parameters."""

    ams_net_id: str
    """Local AMS Net ID (dotted-numeric, e.g. ``"192.168.0.10.1.1"``).
    Empty when unspecified — the driver falls back to the target Net ID."""

    target_net_id: str
    """Target PLC AMS Net ID.  Defaults to ``ams_net_id`` when omitted."""

    target_port: int = 801
    """Target AMS port (TwinCAT 2 default 801; TwinCAT 3 uses 851)."""

    timeout: float = 5.0
    """Operation timeout in seconds."""

    reconnect_max_retries: int = 5
    """Maximum consecutive reconnect attempts before entering FAILED state."""

    reconnect_backoff_max: float = 30.0
    """Upper bound (seconds) for exponential reconnect backoff."""

    twincat_version: str = "2"
    """TwinCAT runtime version: ``'2'`` or ``'3'``.  Drives the default
    ``target_port`` (801 / 851) when no explicit port is given.  Defaults
    to TwinCAT 2 (port 801) for compatibility with older wind-farm PLCs."""

    read_mode: str = "sum"
    """Batch-read strategy: ``'sum'`` (single Sum command) or
    ``'sequential'`` (per-point Read, concurrency-limited)."""

    max_subs_per_sum: int = 500
    """Maximum sub-commands packed into one ADS Sum read; larger point
    sets are split into multiple Sum commands."""

    max_concurrent_reads: int = 16
    """Concurrency limit (semaphore) for ``'sequential'`` reads."""

    subscribe_enabled: bool = False
    """Whether device-notification subscription is enabled."""

    max_delay: float = 0.06
    """Maximum notification delay in seconds."""

    max_notifications_per_connection: int = 550
    """Notification-handle budget per subscription connection."""


def _is_valid_ams_net_id(net_id: str) -> bool:
    """Return True for a dotted-numeric 6-octet AMS Net ID (``a.b.c.d.e.f``)."""
    parts = net_id.split(".")
    if len(parts) != 6:
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def from_device_config(cfg: DeviceConfig) -> ADSConfig:
    """Build a :class:`ADSConfig` from *cfg*.

    ``ams_net_id`` / ``target_net_id`` are validated only when non-empty, so a
    driver can be instantiated (e.g. for a health check) without a full AMS
    configuration.  A malformed Net ID raises :class:`ConfigError`.

    Raises:
        ConfigError: On a malformed ``ams_net_id`` or ``target_net_id``.
    """
    ext = cfg.endpoint.extensions

    ams_net_id = str(ext.get("ams_net_id", ""))
    if ams_net_id and not _is_valid_ams_net_id(ams_net_id):
        raise ConfigError(
            f"ADS device '{cfg.device_id}': invalid ams_net_id '{ams_net_id}'; "
            f"expected dotted-numeric 'a.b.c.d.e.f'"
        )

    target_net_id = str(ext.get("target_net_id", ams_net_id))
    if target_net_id and not _is_valid_ams_net_id(target_net_id):
        raise ConfigError(
            f"ADS device '{cfg.device_id}': invalid target_net_id '{target_net_id}'; "
            f"expected dotted-numeric 'a.b.c.d.e.f'"
        )

    twincat_version = str(ext.get("twincat_version", "2"))
    if twincat_version not in ("2", "3"):
        raise ConfigError(
            f"ADS device '{cfg.device_id}': twincat_version must be '2' or '3', "
            f"got '{twincat_version}'"
        )
    # TwinCAT 3 uses AMS port 851 by default, TwinCAT 2 uses 801; an explicit
    # ``target_port`` / ``ams_port`` still takes precedence.
    default_port = 851 if twincat_version == "3" else 801

    return ADSConfig(
        ams_net_id=ams_net_id,
        target_net_id=target_net_id,
        target_port=int(ext.get("target_port", ext.get("ams_port", default_port))),
        timeout=float(ext.get("timeout", 5.0)),
        reconnect_max_retries=int(ext.get("reconnect_max_retries", 5)),
        reconnect_backoff_max=float(ext.get("reconnect_backoff_max", 30.0)),
        twincat_version=twincat_version,
        read_mode=cfg.read_mode,
        max_subs_per_sum=int(ext.get("max_subs_per_sum", 500)),
        max_concurrent_reads=int(ext.get("max_concurrent_reads", 16)),
        # 订阅开关与协议参数由 endpoint.extensions 透传；notification 的
        # cycle_time 不再是设备级配置——采集节拍属于 Task（Task.interval），
        # 订阅建立时由调用方传入。
        subscribe_enabled=bool(ext.get("subscribe_enabled", False)),
        max_delay=float(ext.get("max_delay", 0.06)),
        max_notifications_per_connection=int(ext.get("max_notifications_per_connection", 550)),
    )
