"""发布失败必须锁死运行时，避免已变化模型重复推进同一时间步。"""

import pytest

from seahorse.application.command_queue import CommandQueue
from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import FarmSnapshot
from seahorse.infrastructure.config import SeahorseConfig, load_config
from seahorse.infrastructure.factory import create_runtime


class FailingPublisher:
    def publish(self, snapshot: FarmSnapshot) -> None:
        raise ConnectionError("gateway disconnected")


def test_publisher_failure_latches_runtime() -> None:
    from pathlib import Path

    config: SeahorseConfig = load_config(
        Path(__file__).resolve().parents[3] / "configs" / "seahorse.yaml"
    )
    runtime = create_runtime(config, publisher=FailingPublisher())
    with pytest.raises(ConnectionError, match="disconnected"):
        runtime.step()
    with pytest.raises(RuntimeError, match="stopped after a failed step"):
        runtime.step()


def test_unexpected_command_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    config = load_config(Path(__file__).resolve().parents[3] / "configs" / "seahorse.yaml")
    commands = CommandQueue()
    runtime = create_runtime(config, commands=commands)
    first = runtime._service._turbines["d10001"]

    def crash(_command: EquipmentCommand) -> None:
        raise OSError("unrecoverable device error")

    monkeypatch.setattr(type(first), "apply", crash)
    outcome = commands.enqueue(EquipmentCommand("unknown-1", "d10001", "ctrlStop", True))
    with pytest.raises(RuntimeError, match="unrecoverable command"):
        runtime.step()
    with pytest.raises(RuntimeError, match="outcome unknown"):
        outcome.result()
    with pytest.raises(RuntimeError, match="stopped after a failed step"):
        runtime.step()
    with pytest.raises(RuntimeError, match="closed"):
        commands.enqueue(EquipmentCommand("unknown-2", "d10001", "ctrlStop", True))
