"""Kafka per-partition contiguous confirmation and rebalance fencing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PartitionOffsets:
    """Only an active generation can confirm records and advance a commit cursor."""

    partition: int
    generation: int
    next_commit_offset: int
    highest_delivered_offset: int = -1
    _confirmed: set[int] = field(default_factory=set)
    _active: bool = True

    def __post_init__(self) -> None:
        if self.next_commit_offset < 0:
            raise ValueError("committed offset must be nonnegative")
        self.highest_delivered_offset = self.next_commit_offset - 1

    def observe(self, *, generation: int, offset: int) -> None:
        self._require_active(generation)
        if offset < self.next_commit_offset:
            raise ValueError("offset already committed")
        if offset != self.highest_delivered_offset + 1:
            raise ValueError("offset gap or out-of-order delivery")
        self.highest_delivered_offset = offset

    def confirm(self, *, generation: int, offset: int) -> int:
        """Return next safe offset; confirmation does not itself perform Kafka commit."""
        self._require_active(generation)
        if not self.next_commit_offset <= offset <= self.highest_delivered_offset:
            raise ValueError("offset not delivered by current partition owner")
        self._confirmed.add(offset)
        while self.next_commit_offset in self._confirmed:
            self._confirmed.remove(self.next_commit_offset)
            self.next_commit_offset += 1
        return self.next_commit_offset

    def revoke(self, *, generation: int) -> None:
        self._require_active(generation)
        self._active = False
        self._confirmed.clear()

    def _require_active(self, generation: int) -> None:
        if not self._active or generation != self.generation:
            raise RuntimeError("partition generation is no longer owned")
