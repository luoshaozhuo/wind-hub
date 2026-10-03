"""SinkUseCase 显式验证聚合单元测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from wind_hub_server.application.usecase.sink import SinkUseCase


async def test_verify_preserves_per_worker_results() -> None:
    collector_a = AsyncMock()
    collector_a.verify_sink.return_value = {
        "success": True,
        "message": None,
        "queue_depth": 0,
    }
    collector_b = AsyncMock()
    collector_b.verify_sink.return_value = {
        "success": False,
        "message": "broker unavailable",
        "queue_depth": 0,
    }

    directory = MagicMock()
    directory.get.side_effect = {
        "collector-a": collector_a,
        "collector-b": collector_b,
    }.__getitem__

    assignments = MagicMock()
    assignments.worker_ids_for_sink.return_value = ["collector-a", "collector-b"]
    config = MagicMock()
    config.current_config.sinks.sinks = [SimpleNamespace(name="archive")]

    usecase = SinkUseCase(
        directory,
        MagicMock(),
        assignments,
        config,
        MagicMock(),
    )

    result = await usecase.verify("archive")

    assert result.success is False
    assert result.message == "broker unavailable"
    assert result.steps == [
        {
            "stage": "health",
            "worker_id": "collector-a",
            "success": True,
            "message": None,
        },
        {
            "stage": "health",
            "worker_id": "collector-b",
            "success": False,
            "message": "broker unavailable",
        },
    ]
