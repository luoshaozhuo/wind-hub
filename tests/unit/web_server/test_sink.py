"""SinkService 显式验证聚合单元测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from web_server.application.port.worker import SinkOperationResult
from web_server.application.sink.service import SinkService


async def test_verify_preserves_per_worker_results() -> None:
    collector_a = AsyncMock()
    collector_a.verify_sink.return_value = SinkOperationResult(
        success=True,
        message=None,
        queue_depth=0,
    )
    collector_b = AsyncMock()
    collector_b.verify_sink.return_value = SinkOperationResult(
        success=False,
        message="broker unavailable",
        queue_depth=0,
    )

    directory = MagicMock()
    directory.get.side_effect = {
        "collector-a": collector_a,
        "collector-b": collector_b,
    }.__getitem__

    placements = MagicMock()
    placements.worker_ids_for_sink.return_value = ["collector-a", "collector-b"]
    config = MagicMock()
    config.current_config.sinks = {"archive": SimpleNamespace(sink_id="archive")}

    service = SinkService(
        directory,
        MagicMock(),
        placements,
        config,
        MagicMock(),
    )

    result = await service.verify("archive")

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


def test_list_sinks_exposes_no_sink_level_points() -> None:
    """新契约下点属于 Task outputs；Sink 快照不得再携带 sink 级 points。

    即便领域 Sink 上残留解析期点（Collector 侧），Web 快照与 API
    也不暴露该字段，避免前端继续维护已删除的编辑路径。
    """
    from core.application.config_snapshot import ConfigSnapshot
    from core.domain.sink import Sink
    from web_server.application.sink.service import SinkService

    config = MagicMock()
    config.current_config = MagicMock(spec=ConfigSnapshot)
    config.current_config.sinks = {
        "modbus_scada": Sink(
            sink_id="modbus_scada",
            kind="modbus",
            enabled=False,
            connection={"host": "0.0.0.0", "port": 1502},
            points=({"ref": "wt01.power"},),
        )
    }
    monitoring = MagicMock()
    monitoring.sinks_snapshot.return_value = []
    service = SinkService(
        collectors=MagicMock(),
        monitoring=monitoring,
        placements=MagicMock(),
        config=config,
        admin=MagicMock(),
    )

    row = service.list_sinks()[0]

    assert row.connection == {"host": "0.0.0.0", "port": 1502}
    assert "points" not in row.model_fields
    assert "point_count" not in row.model_fields
