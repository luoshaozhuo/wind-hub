"""wind-hub-server 生命周期辅助函数单元测试。

验证阶段：unit。使用 MagicMock/AsyncMock，不启动真实协议、Runtime 或监听
socket；覆盖 API Server 构造和 SIGHUP reload 委托，不能证明生产启动闭环。
"""

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn

from core.application.config_diff import ConfigSnapshotDiff
from core.infrastructure.diagnostic.correlation import RequestIdFilter, bind_request_id
from web_server.application.config.service import ReloadResult
from web_server.server import (
    _wait_for_shutdown_or_api_exit,
    build_api_server,
    reload_once,
)
from web_server.settings import ServerSettings

pytestmark = pytest.mark.unit

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def restored_uvicorn_loggers() -> object:
    """build_api_server 会调整 uvicorn logger 级别；用后还原避免污染进程状态。"""
    saved = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
        )
        for name in _UVICORN_LOGGERS
    }
    yield None
    for name, (level, handlers, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.level = level
        logger.handlers = handlers
        logger.propagate = propagate


def _settings(log_level: str = "warning") -> ServerSettings:
    return ServerSettings(
        config_dir=Path("configs"),
        host="0.0.0.0",
        port=9000,
        log_level=log_level,
    )


def test_build_api_server_uses_server_settings(restored_uvicorn_loggers: object) -> None:
    """uvicorn Server 应采用 wind-hub-server 的监听与日志参数。"""
    rt = MagicMock()
    server = build_api_server(rt, _settings())

    assert isinstance(server, uvicorn.Server)
    assert server.config.host == "0.0.0.0"
    assert server.config.port == 9000
    assert server.config.log_level == "warning"


def test_build_api_server_routes_uvicorn_logs_through_root(
    restored_uvicorn_loggers: object,
) -> None:
    """log_config=None 时 uvicorn 不得自建 handler；记录须 propagate 到 root。"""
    server = build_api_server(MagicMock(), _settings("info"))

    assert server.config.log_config is None
    for name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        assert uv_logger.handlers == []
        assert uv_logger.propagate is True
        assert uv_logger.level == logging.INFO


def test_uvicorn_access_records_carry_request_id_to_root(
    restored_uvicorn_loggers: object,
) -> None:
    """uvicorn.access 经 root handler 输出时必须被 RequestIdFilter 注入 request_id。"""
    build_api_server(MagicMock(), _settings("info"))
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = _Capture()
    # 与 configure_process_logging 一致：request_id 由 handler 上的 filter 注入。
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    try:
        with bind_request_id("req-uvicorn-1"):
            logging.getLogger("uvicorn.access").warning("GET /api/v1/overview 200")
    finally:
        root.removeHandler(handler)

    assert len(records) == 1
    assert records[0].name == "uvicorn.access"
    assert records[0].request_id == "req-uvicorn-1"


def test_reconfigure_logging_applies_new_log_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """system.logs 变更时重配进程日志，并重挂 LogStore 缓冲 handler。"""
    import web_server.server as server_module
    from core.application.settings import LogSettings

    applied: list[LogSettings] = []
    monkeypatch.setattr(
        server_module,
        "configure_process_logging",
        lambda settings, process_name: applied.append(settings),
    )
    log_store = MagicMock()
    old = LogSettings()
    new = LogSettings(level="DEBUG")
    candidate = MagicMock()
    candidate.system.logs = new
    previous = MagicMock()
    previous.system.logs = old

    changed = server_module.reconfigure_logging_on_commit(log_store, previous, candidate)

    assert changed is True
    assert applied == [new]
    log_store.uninstall.assert_called_once_with()
    log_store.install.assert_called_once_with()


def test_reconfigure_logging_skips_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import web_server.server as server_module
    from core.application.settings import LogSettings

    def _boom(settings: object, process_name: str) -> None:
        raise AssertionError("must not reconfigure")

    monkeypatch.setattr(server_module, "configure_process_logging", _boom)
    log_store = MagicMock()
    snapshot = MagicMock()
    snapshot.system.logs = LogSettings()

    changed = server_module.reconfigure_logging_on_commit(log_store, snapshot, snapshot)

    assert changed is False
    log_store.uninstall.assert_not_called()
    log_store.install.assert_not_called()


def test_reconfigure_logging_reinstalls_store_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web_server.server as server_module
    from core.application.settings import LogSettings

    def _boom(settings: object, process_name: str) -> None:
        raise PermissionError("cannot create log dir")

    monkeypatch.setattr(server_module, "configure_process_logging", _boom)
    log_store = MagicMock()
    previous = MagicMock()
    previous.system.logs = LogSettings()
    candidate = MagicMock()
    candidate.system.logs = LogSettings(path="/forbidden/path")

    # 异常交给 ConfigService 的钩子隔离记录；缓冲 handler 必须先于异常重挂。
    with pytest.raises(PermissionError):
        server_module.reconfigure_logging_on_commit(log_store, previous, candidate)
    log_store.uninstall.assert_called_once_with()
    log_store.install.assert_called_once_with()


async def test_reload_once_delegates_to_config_use_case() -> None:
    """SIGHUP helper 只调用一次 ConfigService.reload。"""
    config = AsyncMock()
    config.reload.return_value = ReloadResult(
        success=True,
        diff=ConfigSnapshotDiff(
            sections={}, system_changed=False, site_changed=False, units_changed=False
        ),
        errors=[],
        duration_ms=1.0,
    )

    await reload_once(config)

    config.reload.assert_awaited_once()


async def test_wait_for_shutdown_returns_when_signal_arrives() -> None:
    shutdown = asyncio.Event()
    api_task = asyncio.create_task(asyncio.sleep(60))
    shutdown.set()

    api_exited = await _wait_for_shutdown_or_api_exit(shutdown, api_task)

    assert api_exited is False
    api_task.cancel()
    await asyncio.gather(api_task, return_exceptions=True)


async def test_wait_for_shutdown_detects_api_exit() -> None:
    shutdown = asyncio.Event()

    async def finished() -> None:
        return None

    api_task = asyncio.create_task(finished())

    api_exited = await _wait_for_shutdown_or_api_exit(shutdown, api_task)

    assert api_exited is True
