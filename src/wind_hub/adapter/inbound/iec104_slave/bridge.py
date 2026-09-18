"""AcquisitionEngine ↔ Dispatcher bridge for the IEC104 slave proxy.

Decouples the inbound slave adapter from the two extension surfaces it needs:

* **collection** — :meth:`SlaveBridge.on_points_collected` is registered
  as an :class:`~wind_hub.domain.acquisition.AcquisitionEngine` observer
  and keeps the :class:`DataSnapshot` fresh;
* **command** — :meth:`SlaveBridge.forward_command` routes a remote
  control command through the :class:`Dispatcher` and returns its result.
"""

from __future__ import annotations

from wind_hub.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub.domain.command.dispatcher import Dispatcher
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.point import PointValue


class SlaveBridge:
    """Adapts the engine observer stream and the Dispatcher behind a small,
    typed surface."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        snapshot: DataSnapshot,
        mapping: dict[tuple[str, str], int],
    ) -> None:
        self._dispatcher = dispatcher
        self._snapshot = snapshot
        self._mapping = mapping

    def on_points_collected(self, values: list[PointValue]) -> None:
        """Engine observer — refresh the snapshot with freshly collected values."""
        self._snapshot.update(values, self._mapping)

    async def forward_command(self, cmd: Command) -> CommandResult:
        """Dispatch a remote command and return its outcome."""
        return await self._dispatcher.send(cmd)
