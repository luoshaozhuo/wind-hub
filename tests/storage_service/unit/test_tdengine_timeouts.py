"""TDengine 整体期限、故障连接隔离和有界线程回收。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from storage_service.application.config import StorageConfig
from storage_service.application.query import HistoryFilter
from storage_service.domain.sample import StorageSample
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend

pytestmark = pytest.mark.unit


class _Statement:
    def __init__(self, module: _Module) -> None:
        self.module = module

    def _wait(self, stage: str) -> None:
        if self.module.statement_stage == stage:
            self.module.statement_started.set()
            assert self.module.statement_release.wait(timeout=5)

    def prepare(self, sql: str) -> None:
        self._wait("prepare")

    def bind(self, params: list[object]) -> None:
        self._wait("bind")

    def execute(self) -> int:
        self._wait("execute")
        return 1

    def close(self) -> None:
        self._wait("close")


class _Connection:
    def __init__(self, module: _Module) -> None:
        self.module = module
        self.closed = threading.Event()

    def stmt2_statement(self) -> _Statement:
        return _Statement(self.module)

    def query(self, sql: str) -> list[object]:
        index = self.module.connections.index(self)
        if index == 0 and self.module.query_started is not None:
            self.module.query_started.set()
            assert self.module.query_release is not None
            assert self.module.query_release.wait(timeout=5)
        if index == 0 and self.module.query_error is not None:
            raise self.module.query_error
        return []

    def close(self) -> None:
        self.closed.set()


class _Module:
    def __init__(
        self,
        *,
        blocking_connect: bool = False,
        blocking_query: bool = False,
        query_error: OSError | None = None,
        statement_stage: str | None = None,
    ) -> None:
        self.connections: list[_Connection] = []
        self.read_timeouts: list[str] = []
        self.connect_started = threading.Event()
        self.connect_release = threading.Event()
        self.blocking_connect = blocking_connect
        self.query_started = threading.Event() if blocking_query else None
        self.query_release = threading.Event()
        self.query_error = query_error
        self.statement_stage = statement_stage
        self.statement_started = threading.Event()
        self.statement_release = threading.Event()

    def stmt2_bind_param_view(self, **kwargs: object) -> object:
        return object()

    def __getattr__(self, name: str) -> object:
        if name.endswith("_to_column") or name == "micros_timestamps_to_column":
            return lambda values: values
        raise AttributeError(name)

    def connect(self, **kwargs: object) -> _Connection:
        self.read_timeouts.append(cast(str, kwargs["read_timeout"]))
        self.connect_started.set()
        if self.blocking_connect:
            assert self.connect_release.wait(timeout=5)
        connection = _Connection(self)
        self.connections.append(connection)
        return connection


def _config(connections: int = 1) -> StorageConfig:
    return StorageConfig.model_validate(
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "service_id": "storage-timeout-test",
            "allowed_wind_farms": ["farm"],
            "kafka": {
                "bootstrap_servers": "127.0.0.1:9092",
                "topic": "history",
                "group_id": "storage-timeout-test",
            },
            "grpc": {},
            "auth": {
                "callers": [
                    {
                        "token_env": "WIND_HUB_TEST_STORAGE_TOKEN",
                        "permissions": ["storage.read"],
                        "wind_farms": ["farm"],
                    }
                ]
            },
            "tdengine": {
                "endpoint": "127.0.0.1:6041",
                "credential_ref": "env:WIND_HUB_TEST_TDENGINE_PASSWORD",
                "database": {},
                "max_connections": connections,
                "max_inflight_sql": connections,
                "write_timeout_ms": 100,
            },
            "configuration": {},
            "query": {},
            "export": {"root": "/tmp/storage-timeout-exports"},
            "backup": {"root": "/tmp/storage-timeout-backups"},
            "retry": {},
            "reconciliation": {"owner_instance_id": "storage-timeout-test"},
        }
    )


def _sample() -> StorageSample:
    payload = {
        "schema_version": 2,
        "wind_farm_id": "farm",
        "collector_id": "collector",
        "task_id": "task",
        "batch_id": "batch",
        "device_type": "WTG",
        "device_id": "device",
        "point_table_id": "point-table",
        "point_id": "point",
        "business_point_id": "",
        "sample_id": "sample",
        "data_level": "raw",
        "quality": "good",
        "unit": "",
        "value": 1,
        "sample_time": "2027-01-15T16:00:00Z",
        "publish_time": "2027-01-15T16:00:00.000001Z",
        "receive_time": "2027-01-15T16:00:00.000002Z",
    }
    return StorageSample.from_kafka_json(json.dumps(payload).encode())


async def _backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: _Module,
    *,
    connections: int = 1,
) -> TDengineBackend:
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    monkeypatch.setenv("WIND_HUB_TEST_TDENGINE_PASSWORD", "taosdata")
    monkeypatch.setattr(
        "storage_service.infrastructure.adapters.outbound.tdengine_backend.import_module",
        lambda name: cast(Any, module),
    )
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.open()
    backend = TDengineBackend(config=_config(connections), metadata=metadata)
    backend._metadata = metadata
    return backend


async def test_sql_timeout_never_returns_connection_and_recovers_with_bounded_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _Module(blocking_query=True)
    backend = await _backend(tmp_path, monkeypatch, module)
    await backend.open()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            await backend.fetch_content_hashes([_sample()])
        assert module.query_started is not None and module.query_started.is_set()

        # 超时调用不占新连接，也不能靠新线程绕过一个卡住的驱动调用。
        with pytest.raises(TimeoutError, match="deadline"):
            await backend.fetch_content_hashes([_sample()])
        assert len(module.connections) == 1
        assert not module.connections[0].closed.is_set()

        module.query_release.set()
        assert await asyncio.to_thread(module.connections[0].closed.wait, 1)
        assert await backend.fetch_content_hashes([_sample()]) == {
            _sample().logical_key: frozenset()
        }
        assert len(module.connections) == 2
        assert module.read_timeouts == ["1", "1"]
        assert not module.connections[1].closed.is_set()
    finally:
        module.query_release.set()
        await backend.close()
        backend._metadata.close()
    assert all(connection.closed.is_set() for connection in module.connections)


@pytest.mark.parametrize("stage", ["prepare", "bind", "execute", "close"])
async def test_stmt2_every_driver_stage_has_deadline_and_discards_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    module = _Module(statement_stage=stage)
    backend = await _backend(tmp_path, monkeypatch, module)
    await backend.open()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            await backend.insert_batch([_sample()])
        assert module.statement_started.is_set()
        assert not module.connections[0].closed.is_set()
        module.statement_release.set()
        assert await asyncio.to_thread(module.connections[0].closed.wait, 1)
        module.statement_stage = None
        assert await backend.insert_batch([_sample()]) is None
        assert len(module.connections) == 2
    finally:
        module.statement_release.set()
        await backend.close()
        backend._metadata.close()


async def test_connect_timeout_closes_late_connection_instead_of_publishing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _Module(blocking_connect=True)
    backend = await _backend(tmp_path, monkeypatch, module)
    with pytest.raises(TimeoutError, match="deadline"):
        await backend.open()
    assert module.connect_started.is_set()
    assert len(module.connections) == 0
    module.connect_release.set()
    assert await asyncio.to_thread(module.connect_started.wait, 1)
    await backend.close()
    for _ in range(20):
        if module.connections and module.connections[0].closed.is_set():
            break
        await asyncio.sleep(0.01)
    assert len(module.connections) == 1
    assert module.connections[0].closed.is_set()
    backend._metadata.close()


async def test_connection_error_is_unhealthy_and_next_operation_uses_new_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _Module(query_error=OSError("websocket disconnected"))
    backend = await _backend(tmp_path, monkeypatch, module)
    await backend.open()
    try:
        with pytest.raises(OSError, match="disconnected"):
            await backend.fetch_content_hashes([_sample()])
        assert module.connections[0].closed.is_set()
        assert await backend.fetch_content_hashes([_sample()]) == {
            _sample().logical_key: frozenset()
        }
        assert len(module.connections) == 2
    finally:
        await backend.close()
        backend._metadata.close()


async def test_close_deadline_does_not_reclaim_outstanding_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _Module(blocking_query=True)
    backend = await _backend(tmp_path, monkeypatch, module)
    await backend.open()
    operation = asyncio.create_task(backend.fetch_content_hashes([_sample()]))
    assert module.query_started is not None
    assert await asyncio.to_thread(module.query_started.wait, 1)
    with pytest.raises(TimeoutError, match="deadline"):
        await backend.close()
    assert not module.connections[0].closed.is_set()
    module.query_release.set()
    assert await asyncio.to_thread(module.connections[0].closed.wait, 1)
    with pytest.raises(TimeoutError, match="deadline"):
        await operation
    backend._metadata.close()


async def test_multi_connection_close_drains_idle_when_sql_workers_are_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _Module(blocking_query=True)
    backend = await _backend(tmp_path, monkeypatch, module, connections=2)
    await backend.open()
    query_operation = asyncio.create_task(backend.fetch_content_hashes([_sample()]))
    assert module.query_started is not None
    assert await asyncio.to_thread(module.query_started.wait, 1)

    # 第二个工作线程在借连接前阻塞，留下一个空闲连接；旧实现会让 close 排队并漏掉它。
    history_started = threading.Event()
    history_release = threading.Event()
    original_history = backend._fetch_history_sync

    def blocked_history(*args: Any, **kwargs: Any) -> Any:
        history_started.set()
        assert history_release.wait(timeout=5)
        return original_history(*args, **kwargs)

    monkeypatch.setattr(backend, "_fetch_history_sync", blocked_history)
    history_operation = asyncio.create_task(
        backend.fetch_history(
            HistoryFilter(
                wind_farm_id="farm",
                data_level=_sample().data_level,
                start_us=0,
                end_us=1,
            ),
            after=None,
            limit=1,
        )
    )
    assert await asyncio.to_thread(history_started.wait, 1)

    with pytest.raises(TimeoutError, match="deadline"):
        await backend.close()
    assert module.connections[1].closed.is_set()
    assert not module.connections[0].closed.is_set()

    # 重复关闭是幂等空操作；关闭后的公开操作不能触碰已 shutdown 的执行器。
    await backend.close()
    with pytest.raises(RuntimeError, match="closed"):
        await backend.fetch_content_hashes([_sample()])

    module.query_release.set()
    history_release.set()
    assert await asyncio.to_thread(module.connections[0].closed.wait, 1)
    results = await asyncio.gather(query_operation, history_operation, return_exceptions=True)
    assert all(isinstance(result, TimeoutError) for result in results)
    assert len(module.connections) == 2
    assert all(connection.closed.is_set() for connection in module.connections)
    backend._metadata.close()
