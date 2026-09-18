"""Unit tests for the process entry point (``main.py``) helpers.

``run_engine`` 阻塞直到收到信号，不适合在此直接测试；这里只覆盖可无副作用
构造的辅助函数 ``_start_api`` 与 ``_handle_sighup``。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import uvicorn

from wind_hub.domain.model.reload import ConfigDiff, ReloadResult
from wind_hub.main import _handle_sighup, _start_api


def test_start_api_returns_embedded_uvicorn_server() -> None:
    rt = MagicMock()
    rt.runtime.device_count = 2
    rt.runtime.sink_count = 1
    server = _start_api(rt, "127.0.0.1", 8080)
    assert isinstance(server, uvicorn.Server)


async def test_handle_sighup_calls_config_reload() -> None:
    config_service = AsyncMock()
    config_service.reload.return_value = ReloadResult(
        success=True,
        diff=ConfigDiff(),
        errors=[],
        duration_ms=1.0,
    )
    await _handle_sighup(config_service)
    config_service.reload.assert_awaited_once()
