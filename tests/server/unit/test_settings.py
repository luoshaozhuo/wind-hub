"""wind-hub-server ServerSettings 单元测试。

验证阶段：unit。仅验证进程宿主参数的纯数据校验，不读取真实配置、不启动
Runtime、不监听网络端口，因此不能证明现场配置或服务启动闭环。
"""

from pathlib import Path

import pytest

from wind_hub_server.settings import ServerSettings


def test_from_values_normalizes_config_dir() -> None:
    """字符串配置目录应收敛为 Path，其他默认值保持稳定。"""
    settings = ServerSettings.from_values("configs/template")

    assert settings.config_dir == Path("configs/template")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.shutdown_timeout == 30.0


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_is_rejected(port: int) -> None:
    """超出 TCP 端口范围的配置必须在启动前失败。"""
    with pytest.raises(ValueError, match="port"):
        ServerSettings.from_values("configs/template", port=port)


def test_non_positive_shutdown_timeout_is_rejected() -> None:
    """停机硬超时必须为正数，避免形成无限或无效等待语义。"""
    with pytest.raises(ValueError, match="shutdown_timeout"):
        ServerSettings.from_values("configs/template", shutdown_timeout=0)
