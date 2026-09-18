"""Processor registry — global singleton for processor lookup by name.

Mirrors :mod:`wind_hub.infra.registry` (the protocol-driver registry):
processors self-register at module-import time via the
:func:`register_processor` decorator, and the composition root creates
instances by name through :meth:`create` using the names listed in
``system.yaml``'s ``pipeline.processors``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from wind_hub.domain.model.errors import ConfigError

if TYPE_CHECKING:
    from wind_hub.domain.port.outbound import ProcessorPort


class ProcessorRegistry:
    """Processor registry — name → factory lookup."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], ProcessorPort]] = {}

    def register(self, name: str, factory: Callable[[], ProcessorPort]) -> None:
        """Register a processor factory.

        Args:
            name: Processor name (must match ``pipeline.processors`` entries).
            factory: Zero-arg callable returning a ``ProcessorPort`` instance.

        Raises:
            ConfigError: If *name* is already registered.
        """
        if name in self._factories:
            raise ConfigError(f"Processor '{name}' is already registered")
        self._factories[name] = factory

    def create(self, name: str) -> ProcessorPort:
        """Instantiate a processor by name.

        Returns:
            A ``ProcessorPort`` instance.

        Raises:
            ConfigError: If *name* is not registered.
        """
        factory = self._factories.get(name)
        if factory is None:
            raise ConfigError(
                f"Unknown processor '{name}'. " f"Registered processors: {sorted(self._factories)}"
            )
        return factory()

    def names(self) -> list[str]:
        """Return all registered processor names, sorted."""
        return sorted(self._factories)

    def is_registered(self, name: str) -> bool:
        """Return ``True`` when *name* has a registered factory."""
        return name in self._factories


# ---------------------------------------------------------------------------
# global singleton
# ---------------------------------------------------------------------------

processor_registry = ProcessorRegistry()


def register_processor(
    name: str,
) -> Callable[[Callable[[], ProcessorPort]], Callable[[], ProcessorPort]]:
    """Decorator that auto-registers a processor factory.

    Usage (module bottom)::

        @register_processor("unit_convert")
        def _create() -> ProcessorPort:
            return UnitConvertProcessor()
    """

    def _decorator(
        factory: Callable[[], ProcessorPort],
    ) -> Callable[[], ProcessorPort]:
        processor_registry.register(name, factory)
        return factory

    return _decorator
