"""Data snapshot — the slave proxy's in-memory IOA → PointValue store.

Pure logic: no I/O, no asyncio, no threading primitives.  The owning event
loop is strictly single-threaded, so a plain ``dict`` is the only backing
store; updates and reads are plain assignments and lookups.
"""

from __future__ import annotations

from wind_hub.domain.model.point import PointValue


class DataSnapshot:
    """Latest-value cache keyed by IEC104 information-object address.

    The proxy updates this snapshot as the engine collects, so that a
    general interrogation can serve the most recent value for every
    exposed point without touching the device itself.
    """

    def __init__(self) -> None:
        self._snapshot: dict[int, PointValue] = {}

    def update(self, values: list[PointValue], mapping: dict[tuple[str, str], int]) -> None:
        """Store *values* for the points present in *mapping*.

        ``mapping`` translates ``(device_id, point_id)`` to IOA; values
        whose point is not exposed by the reporting config are ignored.
        """
        for pv in values:
            ioa = mapping.get((pv.device_id, pv.point_id))
            if ioa is None:
                continue
            self._snapshot[ioa] = pv

    def get(self, ioa: int) -> PointValue | None:
        """Return the latest value for *ioa*, or ``None``."""
        return self._snapshot.get(ioa)

    def get_all(self) -> list[tuple[int, PointValue]]:
        """Return every ``(ioa, PointValue)`` pair, sorted by IOA ascending."""
        return sorted(self._snapshot.items(), key=lambda item: item[0])

    @property
    def size(self) -> int:
        """Number of points currently held in the snapshot."""
        return len(self._snapshot)
