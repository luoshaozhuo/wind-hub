"""Outbound ports — extension-point interfaces implemented by adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.point import PointRef, PointValue

if TYPE_CHECKING:
    from wind_hub.config.schema import PointConfig


class HealthStatus(BaseModel):
    """Minimal health-check placeholder — refined in a later step."""

    healthy: bool
    """``True`` when the component is operating normally."""

    message: str | None = None
    """Optional human-readable status detail (e.g. error description)."""


class ProtocolPort(Protocol):
    """Protocol extension point.

    Implementations handle the wire protocol for a specific device
    family: ADS, Modbus, IEC104, etc.
    """

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """Inject the device's point table; the driver builds an
        ``address ↔ point_id`` mapping for later reads/writes.

        Pure in-memory and synchronous — no network I/O.  A driver
        that has no notion of a point table may implement this as a
        no-op.

        Args:
            points: The full point table for this device, from config.
        """
        ...

    async def connect(self) -> None:
        """Establish the underlying transport connection(s).

        Raises:
            ProtocolError: On connection failure.
        """
        ...

    async def close(self) -> None:
        """Tear down the transport connection(s).

        Must be safe to call even if already closed.
        """
        ...

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """Batch-read multiple points.

        Args:
            points: Points to read.

        Returns:
            One ``PointValue`` per input ``PointRef``, in the same order.

        Raises:
            ProtocolError: If any read fails.
        """
        ...

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """Batch-write multiple commands.

        Every input ``Command`` yields one ``CommandResult`` at the
        corresponding list index.

        Args:
            cmds: Commands to execute.

        Returns:
            One ``CommandResult`` per command, in input order.

        Raises:
            ProtocolError: On transport-level failure.
        """
        ...

    async def subscribe(
        self, points: list[PointRef], callback: Callable[[PointValue], Awaitable[None]]
    ) -> None:
        """Subscribe to spontaneous updates for the given points.

        When a device pushes data (e.g. IEC104 change-of-state), the
        adapter calls ``callback(value)`` for each update.

        Args:
            points: Points to subscribe to.
            callback: Async callable invoked with each ``PointValue``.

        Raises:
            NotImplementedError: If the protocol does not support
                subscription (e.g. pure-polling drivers).
        """
        ...

    def health(self) -> HealthStatus:
        """Return connection health status — synchronous by design.

        Callers poll this cheaply; the implementation returns cached
        state rather than probing the wire.
        """
        ...


class SinkPort(Protocol):
    """Output extension point.

    Implementations persist or forward batches of ``PointValue``
    to external systems: Kafka topics, files, databases, etc.
    """

    async def open(self) -> None:
        """Initialise the sink (open files, connect to broker, …).

        Raises:
            SinkError: On initialisation failure.
        """
        ...

    async def close(self) -> None:
        """Finalise the sink (flush buffers, close connections).

        Must be safe to call even if already closed.
        """
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """Write a batch of point values.

        The batch contains only data that the router assigned to this
        sink — the sink does not need to know about routing rules.

        Args:
            batch: Point values to persist/forward.

        Raises:
            SinkError: On write failure.
        """
        ...

    async def flush(self) -> None:
        """Force-flush buffered data to the underlying medium.

        Used during graceful shutdown to ensure no data loss.

        Raises:
            SinkError: On flush failure.
        """
        ...

    def health(self) -> HealthStatus:
        """Return sink health status — synchronous, returns cached state."""
        ...


class ProcessorPort(Protocol):
    """Processing extension point.

    Processors transform a batch of ``PointValue`` instances in place
    — filtering, enriching, or computing derived values.  They are
    chained together in the order specified by configuration.
    """

    @property
    def name(self) -> str:
        """Unique processor name used for logging and configuration."""
        ...

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        """Transform a batch of point values.

        The output list may differ in length from the input (filters
        drop values; derived-point processors add values).

        Args:
            batch: Input point values.

        Returns:
            Transformed point values.

        Raises:
            ProcessorError: On any transformation failure (e.g.
                external service enrichment fails).
        """
        ...


@runtime_checkable
class PointsConfigurable(Protocol):
    """An optional capability of a ``ProcessorPort`` implementation.

    Processors that transform values based on per-point configuration
    (scale/offset, deadband, min/max bounds) implement this to receive
    the full point table at assembly time, before any batch is processed.
    Processors without per-point config simply do not implement it; the
    composition root probes for this capability before injecting points.
    """

    def set_points_config(self, points: list[PointConfig]) -> None:
        """Inject the point table; build any per-point lookup tables.

        Pure in-memory and synchronous — no I/O.  Called once by the
        composition root after the processor is created.

        Args:
            points: The full point table from ``points.yaml``.
        """
        ...
