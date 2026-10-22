"""命令排队的去重、容量及顺序语义。"""
import pytest

from seahorse.application.command_queue import CommandQueue
from seahorse.domain.commands import EquipmentCommand


def test_queue_order_and_replay_rejection() -> None:
    queue = CommandQueue(max_pending=2)
    first = EquipmentCommand("a", "d10001", "ctrlStop", True)
    second = EquipmentCommand("b", "d10001", "ctrlStart", True)
    queue.enqueue(first)
    queue.enqueue(second)
    assert queue.drain() == (first, second)
    with pytest.raises(ValueError, match="duplicate"):
        queue.enqueue(first)


def test_queue_capacity() -> None:
    queue = CommandQueue(max_pending=1)
    queue.enqueue(EquipmentCommand("a", "d10001", "ctrlStop", True))
    with pytest.raises(RuntimeError, match="full"):
        queue.enqueue(EquipmentCommand("b", "d10001", "ctrlStop", True))
