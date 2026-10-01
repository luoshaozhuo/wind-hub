"""wind-hub-server 生命周期辅助函数单元测试。

验证阶段：unit。使用 MagicMock/AsyncMock，不启动真实协议、Runtime 或监听
socket；覆盖 API Server 构造和 SIGHUP reload 委托，不能证明生产启动闭环。
"""

from unittest.mock import AsyncMock, MagicMock

import uvicorn

from wind_hub.domain.model.reload import ConfigDiff, ReloadResult
from wind_hub_server.server import build_api_server, reload_once
from wind_hub_server.settings import ServerSettings


def test_build_api_server_uses_server_settings() -> None:
    """uvicorn Server 应采用 wind-hub-server 的监听与日志参数。"""
    rt = MagicMock()
    rt.runtime.device_count = 2
    rt.runtime.sink_count = 1
    settings = ServerSettings.from_values(
        "configs/template",
        host="0.0.0.0",
        port=9000,
        log_level="warning",
    )

    server = build_api_server(rt, settings)

    assert isinstance(server, uvicorn.Server)
    assert server.config.host == "0.0.0.0"
    assert server.config.port == 9000
    assert server.config.log_level == "warning"


async def test_reload_once_delegates_to_config_use_case() -> None:
    """SIGHUP helper 只调用一次 ConfigUseCase.reload。"""
    config = AsyncMock()
    config.reload.return_value = ReloadResult(
        success=True,
        diff=ConfigDiff(),
        errors=[],
        duration_ms=1.0,
    )

    await reload_once(config)

    config.reload.assert_awaited_once()
