"""PartitionOffset 连续确认、缺口与代际隔离。"""

from __future__ import annotations

import pytest

from storage_service.application.offsets import PartitionOffsets

pytestmark = pytest.mark.unit


def test_only_contiguous_confirmation_advances_commit_cursor() -> None:
    offsets = PartitionOffsets(partition=0, generation=7, next_commit_offset=10)
    for offset in (10, 11, 12):
        offsets.observe(generation=7, offset=offset)

    assert offsets.confirm(generation=7, offset=11) == 10
    assert offsets.confirm(generation=7, offset=10) == 12
    assert offsets.confirm(generation=7, offset=12) == 13
    assert offsets.next_commit_offset == 13


def test_offset_gap_replay_and_out_of_order_delivery_are_rejected() -> None:
    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=5)
    with pytest.raises(ValueError, match="offset gap or out-of-order"):
        offsets.observe(generation=1, offset=6)

    offsets.observe(generation=1, offset=5)
    with pytest.raises(ValueError, match="offset gap or out-of-order"):
        offsets.observe(generation=1, offset=5)
    with pytest.raises(ValueError, match="not delivered"):
        offsets.confirm(generation=1, offset=6)


def test_revoked_generation_cannot_confirm_or_mutate_replacement() -> None:
    old = PartitionOffsets(partition=0, generation=1, next_commit_offset=0)
    old.observe(generation=1, offset=0)
    old.revoke(generation=1)
    replacement = PartitionOffsets(partition=0, generation=2, next_commit_offset=0)

    with pytest.raises(RuntimeError, match="no longer owned"):
        old.confirm(generation=1, offset=0)
    with pytest.raises(RuntimeError, match="no longer owned"):
        old.observe(generation=1, offset=1)
    with pytest.raises(RuntimeError, match="no longer owned"):
        old.revoke(generation=1)

    replacement.observe(generation=2, offset=0)
    assert replacement.confirm(generation=2, offset=0) == 1
    assert old.next_commit_offset == 0


def test_invalid_initial_cursor_and_stale_generation_are_rejected() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        PartitionOffsets(partition=0, generation=1, next_commit_offset=-1)
    offsets = PartitionOffsets(partition=0, generation=2, next_commit_offset=0)
    with pytest.raises(RuntimeError, match="no longer owned"):
        offsets.observe(generation=1, offset=0)
