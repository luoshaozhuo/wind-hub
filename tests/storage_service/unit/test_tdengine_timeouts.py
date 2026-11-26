"""Bounded TDengine connection pool, deadlines and STMT2 fault isolation."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


class _Statement:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver

    def _stage(self, stage: str) -> None:
        if self.driver.statement_stage == stage:
            self.driver.statement_started.set()
            assert self.driver.statement_release.wait(timeout=5)

    def prepare(self, sql: str) -> None:
        self._stage("prepare")

    def bind(self, values: list[object]) -> None:
        self._stage("bind")

    def execute(self) -> int:
        self._stage("execute")
        return 1

    def close(self) -> None:
        self._stage("close")


class _Connection:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver
        self.closed = threading.Event()

    def stmt2_statement(self) -> _Statement:
        return _Statement(self.driver)

    def query(self, sql: str) -> list[object]:
        if self.driver.connections.index(self) == 0:
            if self.driver.query_started is not None:
                self.driver.query_started.set()
                assert self.driver.query_release.wait(timeout=5)
            if self.driver.query_error is not None:
                error = self.driver.query_error
                self.driver.query_error = None
                raise error
        return []

    def close(self) -> None:
        self.closed.set()


class _Driver:
    def __init__(
        self,
        *, blocking_connect: bool = False, blocking_query: bool = False,
        query_error: OSError | None = None, statement_stage: str | None = None,
    ) -> None:
        self.connections: list[_Connection] = []
        self.read_timeouts: list[str] = []
        self.blocking_connect = blocking_connect
        self.connect_started = threading.Event()
        self.connect_release = threading.Event()
        self.query_started = threading.Event() if blocking_query else None
        self.query_release = threading.Event()
        self.query_error = query_error
        self.statement_stage = statement_stage
        self.statement_started = threading.Event()
        self.statement_release = threading.Event()

    def connect(self, **kwargs: object) -> _Connection:
        self.read_timeouts.append(cast(str, kwargs["read_timeout"]))
        self.connect_started.set()
        if self.blocking_connect:
            assert self.connect_release.wait(timeout=5)
        connection = _Connection(self)
        self.connections.append(connection)
        return connection


def _configuration(connections: int) -> StorageConfig:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    ingestion = config.tdengine.ingestion.model_copy(update={"write_timeout_ms": 100})
    tdengine = config.tdengine.model_copy(update={
        "max_connections": connections,
        "max_inflight_sql": connections,
        "ingestion": ingestion,
    })
    return config.model_copy(update={"tdengine": tdengine})


async def _backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: _Driver,
    *, connections: int = 1,
) -> TDengineBackend:
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    monkeypatch.setenv("WIND_HUB_TDENGINE_PASSWORD", "taosdata")
    monkeypatch.setattr(
        "storage_service.infrastructure.adapters.outbound.tdengine_backend.import_module",
        lambda _: cast(Any, driver),
    )
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.open()
    return TDengineBackend(config=_configuration(connections), metadata=metadata)


def _query(backend: TDengineBackend, *, lease: Any) -> list[object]:
    with backend._borrow_connection(lease=lease) as (connection, _):
        return list(connection.query("SELECT 1"))


def _statement(backend: TDengineBackend, *, lease: Any) -> int:
    with backend._borrow_connection(lease=lease) as (connection, _):
        statement = connection.stmt2_statement()
        try:
            statement.prepare("INSERT INTO test VALUES (?)")
            statement.bind([object()])
            return statement.execute()
        finally:
            statement.close()


@pytest.mark.asyncio
async def test_timeout_discards_late_connection_and_allows_bounded_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _Driver(blocking_query=True)
    backend = await _backend(tmp_path, monkeypatch, driver)
    await backend.open()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            await backend._run_bounded(_query, backend)
        assert driver.query_started is not None and driver.query_started.is_set()
        with pytest.raises(TimeoutError, match="deadline"):
            await backend._run_bounded(_query, backend)
        assert len(driver.connections) == 1
        driver.query_release.set()
        assert await asyncio.to_thread(driver.connections[0].closed.wait, 1)
        assert await backend._run_bounded(_query, backend) == []
        assert len(driver.connections) == 2
        assert driver.read_timeouts == ["1", "1"]
    finally:
        driver.query_release.set()
        await backend.close()
        backend._metadata.close()
    assert all(item.closed.is_set() for item in driver.connections)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "bind", "execute", "close"])
async def test_stmt2_stages_obey_same_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    driver = _Driver(statement_stage=stage)
    backend = await _backend(tmp_path, monkeypatch, driver)
    await backend.open()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            await backend._run_bounded(_statement, backend)
        assert driver.statement_started.is_set()
        driver.statement_release.set()
        assert await asyncio.to_thread(driver.connections[0].closed.wait, 1)
        driver.statement_stage = None
        assert await backend._run_bounded(_statement, backend) == 1
        assert len(driver.connections) == 2
    finally:
        driver.statement_release.set()
        await backend.close()
        backend._metadata.close()


@pytest.mark.asyncio
async def test_connection_error_discards_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _Driver(query_error=OSError("websocket disconnected"))
    backend = await _backend(tmp_path, monkeypatch, driver)
    await backend.open()
    try:
        with pytest.raises(OSError, match="disconnected"):
            await backend._run_bounded(_query, backend)
        assert driver.connections[0].closed.is_set()
        assert await backend._run_bounded(_query, backend) == []
        assert len(driver.connections) == 2
    finally:
        await backend.close()
        backend._metadata.close()


@pytest.mark.asyncio
async def test_connect_timeout_closes_late_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _Driver(blocking_connect=True)
    backend = await _backend(tmp_path, monkeypatch, driver)
    with pytest.raises(TimeoutError, match="deadline"):
        await backend.open()
    assert driver.connect_started.is_set()
    driver.connect_release.set()
    await backend.close()
    for _ in range(30):
        if driver.connections and driver.connections[0].closed.is_set():
            break
        await asyncio.sleep(0.01)
    assert len(driver.connections) == 1
    assert driver.connections[0].closed.is_set()
    backend._metadata.close()
