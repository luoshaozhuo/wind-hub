"""发布失败必须锁死运行时，避免已变化模型重复推进同一时间步。"""
import pytest

from seahorse.infrastructure.config import SeahorseConfig, load_config
from seahorse.infrastructure.factory import create_runtime
from seahorse.domain.models import FarmSnapshot


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
