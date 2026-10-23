"""命令队列的串行处理、确认、失败与关闭语义。"""

import pytest

from seahorse.application.command_queue import CommandQueue
from seahorse.domain.commands import EquipmentCommand


def test_queue_order_and_replay_rejection() -> None:
    queue = CommandQueue(max_pending=2)
    first = EquipmentCommand("a", "d10001", "ctrlStop", True)
    second = EquipmentCommand("b", "d10001", "ctrlStart", True)
    accepted_a = queue.enqueue(first)
    accepted_b = queue.enqueue(second)
    items = queue.drain()
    assert tuple(item.command for item in items) == (first, second)
    items[0].outcome.set_result(None)
    items[1].outcome.set_exception(ValueError("rejected"))
    assert accepted_a.result() is None
    with pytest.raises(ValueError, match="rejected"):
        accepted_b.result()
    with pytest.raises(ValueError, match="duplicate"):
        queue.enqueue(first)


def test_queue_capacity_and_close() -> None:
    queue = CommandQueue(max_pending=1)
    outcome = queue.enqueue(EquipmentCommand("a", "d10001", "ctrlStop", True))
    with pytest.raises(RuntimeError, match="full"):
        queue.enqueue(EquipmentCommand("b", "d10001", "ctrlStop", True))
    queue.close()
    with pytest.raises(RuntimeError, match="stopped"):
        outcome.result()
    with pytest.raises(RuntimeError, match="closed"):
        queue.enqueue(EquipmentCommand("c", "d10001", "ctrlStop", True))


def test_command_identity_capacity_fails_closed_without_replay() -> None:
    queue = CommandQueue(max_pending=2, max_command_ids=1)
    original = EquipmentCommand("first", "d10001", "ctrlStop", True)
    queue.enqueue(original)
    queue.drain()
    with pytest.raises(ValueError, match="duplicate"):
        queue.enqueue(original)
    with pytest.raises(RuntimeError, match="identity capacity exhausted"):
        queue.enqueue(EquipmentCommand("second", "d10001", "ctrlStart", True))


def test_command_identity_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="capacities"):
        CommandQueue(max_command_ids=0)
