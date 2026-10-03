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



def test_list_sinks_preserves_resolved_points_for_admin_roundtrip() -> None:
    from wind_hub_core.config.schema import Config
    from wind_hub_core.config.sinks import ResolvedSinkConfig, ResolvedSinksConfig
    from wind_hub_server.application.usecase.sink import SinkUseCase

    config = MagicMock()
    config.current_config = MagicMock(spec=Config)
    config.current_config.sinks = ResolvedSinksConfig(
        sinks=[
            ResolvedSinkConfig(
                name="modbus_scada",
                type="modbus",
                enabled=False,
                connection={"host": "0.0.0.0", "port": 1502},
                points=[
                    {
                        "source": {"device_id": "wt01", "point_id": "power"},
                        "ref": "wt01.power",
                        "source_data_type": "float32",
                        "source_unit": "none",
                        "datatype": "float32",
                        "unit": "none",
                        "address": {
                            "unit_id": 1,
                            "register_type": "holding",
                            "address": 100,
                        },
                    }
                ],
            )
        ]
    )
    monitoring = MagicMock()
    monitoring.sinks_snapshot.return_value = []
    usecase = SinkUseCase(
        collectors=MagicMock(),
        monitoring=monitoring,
        assignments=MagicMock(),
        config=config,
        admin=MagicMock(),
    )

    row = usecase.list_sinks()[0]

    assert row.connection == {"host": "0.0.0.0", "port": 1502}
    assert row.point_count == 1
    assert row.points[0]["ref"] == "wt01.power"
    assert row.points[0]["address"]["address"] == 100
