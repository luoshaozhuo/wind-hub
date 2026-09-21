"""Protocol driver registry — global singleton for driver lookup by name."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from wind_hub.domain.model.errors import ConfigError

if TYPE_CHECKING:
    from wind_hub.config.schema import DeviceConfig
    from wind_hub.domain.port.outbound import ProtocolPort


class ProtocolRegistry:
    """Protocol driver registry.

    Drivers self-register at module import time via the
    :func:`register_protocol` decorator.  ``main.py`` creates driver
    instances by name through :meth:`create`.
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[DeviceConfig], ProtocolPort]] = {}

    def register(
        self,
        name: str,
        factory: Callable[[DeviceConfig], ProtocolPort],
    ) -> None:
        """Register a driver factory.

        Args:
            name: Protocol name (must match ``DeviceConfig.protocol``).
            factory: Callable that receives a ``DeviceConfig`` and
                returns a ``ProtocolPort`` instance.

        Raises:
            ConfigError: If *name* is already registered.
        """
        if name in self._factories:
            raise ConfigError(f"Protocol driver '{name}' is already registered")
        self._factories[name] = factory

    def create(self, name: str, cfg: DeviceConfig) -> ProtocolPort:
        """Instantiate a protocol driver by name.

        Args:
            name: Protocol name.
            cfg: Device configuration for the new driver.

        Returns:
            A ``ProtocolPort`` instance.

        Raises:
            ConfigError: If *name* is not registered.
        """
        factory = self._factories.get(name)
        if factory is None:
            raise ConfigError(
                f"Unknown protocol driver '{name}'. "
                f"Registered drivers: {sorted(self._factories)}"
            )
        return factory(cfg)

    def names(self) -> list[str]:
        """Return all registered driver names."""
        return sorted(self._factories)

    def is_registered(self, name: str) -> bool:
        """Return ``True`` when *name* has a registered factory."""
        return name in self._factories


# ---------------------------------------------------------------------------
# global singleton
# ---------------------------------------------------------------------------

protocol_registry = ProtocolRegistry()


def register_protocol(
    name: str,
) -> Callable[
    [Callable[[DeviceConfig], ProtocolPort]],
    Callable[[DeviceConfig], ProtocolPort],
]:
    """Decorator that auto-registers a protocol driver factory.

    Usage (module bottom)::

        @register_protocol("myproto")
        def _create_myproto(cfg: DeviceConfig) -> ProtocolPort:
            return MyProtoDriver(cfg)
    """

    def _decorator(
        factory: Callable[[DeviceConfig], ProtocolPort],
    ) -> Callable[[DeviceConfig], ProtocolPort]:
        protocol_registry.register(name, factory)
        return factory

    return _decorator
