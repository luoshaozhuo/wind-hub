"""Admin 批量更新 Redis Sink 时不回传已有认证口令。"""

from unittest.mock import AsyncMock

import pytest

from web_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminSinkItem,
    AdminStateService,
)


@pytest.mark.asyncio
async def test_redis_password_is_preserved_without_disclosure() -> None:
    admin = AsyncMock()
    service = AdminStateService(admin)
    state = AdminDefinitionsState(
        units={}, device_types={}, device_models={}, point_tables={}
    )
    await service.replace_all(
        devices=[],
        tasks=[],
        sinks=[
            AdminSinkItem(
                name="redis-1",
                type="redis",
                connection={"host": "localhost", "port": 6379},
            )
        ],
        definitions=state,
    )
    mutate = admin.mutate_yaml_files.call_args.args[1]
    documents = {
        "devices.yaml": {"devices": {}, "device_groups": {}},
        "tasks.yaml": {"tasks": {}},
        "points.yaml": {"point_tables": {}},
        "units.yaml": {"quantities": {}, "units": {}},
        "device_models.yaml": {"device_types": {}, "device_models": {}},
        "sinks.yaml": {
            "sinks": {
                "redis-1": {
                    "type": "redis",
                    "connection": {
                        "host": "localhost",
                        "password": "secret",
                    },
                }
            }
        },
    }
    mutate(documents)
    connection = documents["sinks.yaml"]["sinks"]["redis-1"]["connection"]
    assert connection["host"] == "localhost"
    assert connection["password"] == "secret"


@pytest.mark.asyncio
async def test_redis_password_is_cleared_when_explicit_empty() -> None:
    """password 显式为 "" 表示清空：与缺省保留语义区分开。"""
    admin = AsyncMock()
    service = AdminStateService(admin)
    state = AdminDefinitionsState(
        units={}, device_types={}, device_models={}, point_tables={}
    )
    await service.replace_all(
        devices=[],
        tasks=[],
        sinks=[
            AdminSinkItem(
                name="redis-1",
                type="redis",
                connection={"host": "localhost", "port": 6379, "password": ""},
            )
        ],
        definitions=state,
    )
    mutate = admin.mutate_yaml_files.call_args.args[1]
    documents = {
        "devices.yaml": {"devices": {}, "device_groups": {}},
        "tasks.yaml": {"tasks": {}},
        "points.yaml": {"point_tables": {}},
        "units.yaml": {"quantities": {}, "units": {}},
        "device_models.yaml": {"device_types": {}, "device_models": {}},
        "sinks.yaml": {
            "sinks": {
                "redis-1": {
                    "type": "redis",
                    "connection": {"host": "localhost", "password": "secret"},
                }
            }
        },
    }
    mutate(documents)
    connection = documents["sinks.yaml"]["sinks"]["redis-1"]["connection"]
    assert "password" not in connection
