"""Sink 管理读取不能泄露 Redis 密码。"""

from types import SimpleNamespace

import pytest

from core.domain.sink import Sink
from web_server.application.sink.service import SinkService

pytestmark = pytest.mark.unit


def test_sink_snapshot_does_not_expose_password() -> None:
    sink = Sink(
        sink_id="redis-1",
        kind="redis",
        enabled=True,
        connection={
            "host": "127.0.0.1",
            "port": 6379,
            "password": "hidden-secret",
        },
    )
    config = SimpleNamespace(current_config=SimpleNamespace(sinks={"redis-1": sink}))
    monitor = SimpleNamespace(sinks_snapshot=lambda: [])
    service = SinkService(None, monitor, None, config, None)
    snapshot = service.list_sinks()[0]
    assert snapshot.name == "redis-1"
    assert snapshot.connection["host"] == "127.0.0.1"
    assert "password" not in snapshot.connection
    assert snapshot.has_password is True


def test_sink_snapshot_reports_has_password_without_value() -> None:
    sink = Sink(
        sink_id="redis-2",
        kind="redis",
        enabled=True,
        connection={"host": "127.0.0.1", "port": 6379},
    )
    config = SimpleNamespace(current_config=SimpleNamespace(sinks={"redis-2": sink}))
    monitor = SimpleNamespace(sinks_snapshot=lambda: [])
    service = SinkService(None, monitor, None, config, None)

    snapshot = service.list_sinks()[0]
    assert snapshot.has_password is False
    assert "password" not in snapshot.connection
