"""IEC 60870-5-104 driver configuration.

Parsed from :class:`~wind_hub.config.schema.DeviceConfig` extensions.
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub.config.schema import DeviceConfig


@dataclass(frozen=True)
class IEC104Config:
    """Typed configuration for the IEC104 protocol driver.

    All values are extracted from ``DeviceConfig.endpoint.extensions``
    with sensible defaults defined by IEC 60870-5-104.
    """

    host: str
    """IP address or hostname of the IEC104 slave."""

    port: int
    """TCP port (default 2404)."""

    common_addr: int
    """Common address / station address (CASDU, 1–65535)."""

    k: int
    """Maximum number of unacknowledged I-frames we may send before
    waiting for the peer to acknowledge (default 12)."""

    w: int
    """Maximum number of I-frames we may receive before sending an
    S-frame acknowledgement (default 8)."""

    t0: float
    """Connection-establishment timeout in seconds (default 30)."""

    t1: float
    """Send / confirm timeout in seconds (default 15).
    When we send an I-frame, the peer must acknowledge within t1;
    otherwise the connection is considered broken."""

    t2: float
    """Ack delay timeout in seconds (default 10).
    After receiving an I-frame we must send an S-frame within t2 if
    the *w* threshold hasn't been reached sooner."""

    t3: float
    """Idle / keep-alive timeout in seconds (default 20).
    If no data is received for t3 seconds, a TESTFR act is sent."""

    max_reconnect_retries: int = 5
    """Maximum number of consecutive reconnect attempts before giving
    up and transitioning to FAILED state."""

    @classmethod
    def from_device_config(cls, cfg: DeviceConfig) -> IEC104Config:
        """Build an ``IEC104Config`` from a ``DeviceConfig``.

        Protocol-specific parameters are read from
        ``cfg.endpoint.extensions``, falling back to IEC104 defaults.
        """
        extensions = cfg.endpoint.extensions

        return cls(
            host=cfg.endpoint.host,
            port=cfg.endpoint.port,
            common_addr=int(extensions.get("common_addr", 1)),
            k=int(extensions.get("k", 12)),
            w=int(extensions.get("w", 8)),
            t0=float(extensions.get("t0", 30.0)),
            t1=float(extensions.get("t1", 15.0)),
            t2=float(extensions.get("t2", 10.0)),
            t3=float(extensions.get("t3", 20.0)),
            max_reconnect_retries=int(extensions.get("max_reconnect_retries", 5)),
        )
