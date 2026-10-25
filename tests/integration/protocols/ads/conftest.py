"""ADS 集成测试服务 fixture。

与 ``redis_service`` 同一模式：环境变量指向真实
TwinCAT PLC 时直接使用（不管理其生命周期）；否则在进程内拉起 pyads 自带的
``AdsTestServer``——真实 AMS/TCP 协议栈上的真实 ADS Server，属于
``real_service``，不是 mock。两条路径都不退化协议路径。

注意：pyads 客户端的 TCP 目的端口固定为 48898（AMS 851/801 只是逻辑端口），
因此 testserver 路径的 ``port`` 取 48898，诊断的 TCP 探测才能命中真实监听。
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import pytest

from tests.support.env import ads_config_from_env

#: testserver 路径的固定符号与 AMS 身份；与真实 PLC 路径的 env 键保持一致。
_TESTSERVER_READ_SYMBOL = "Main.temperature"
_TESTSERVER_WRITE_SYMBOL = "Main.setpoint"
_TESTSERVER_NET_ID = "127.0.0.1.2.1"
_TESTSERVER_LOCAL_NET_ID = "127.0.0.1.1.1"
_ADS_TCP_PORT = 48898


def _wait_tcp_port(host: str, port: int, timeout: float = 5.0) -> None:
    """等待 testserver 线程完成 bind/listen。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"ADS testserver did not open {host}:{port} within {timeout}s")


_ADS_SERVICE_CACHE: dict[str, object] | None = None


@pytest.fixture(scope="session")
def ads_service() -> Iterator[dict[str, object]]:
    """ADS 服务端配置（真实 PLC 环境变量优先，否则进程内 AdsTestServer）。

    返回字典键与 :func:`ads_config_from_env` 一致，另加 ``backend``
    （``"hardware"`` / ``"testserver"``）标明当前服务端真实性。

    本 fixture 会被 reliability 层 conftest 以 import 方式复用；pytest 对跨
    conftest 导入的同名 fixture 各自实例化一次，因此这里用模块级缓存保证
    全进程只有一个 AdsTestServer，避免第二次 bind 48898 冲突。
    """
    global _ADS_SERVICE_CACHE
    if _ADS_SERVICE_CACHE is not None:
        yield _ADS_SERVICE_CACHE
        return

    external = ads_config_from_env()
    if external is not None:
        external["backend"] = "hardware"
        _ADS_SERVICE_CACHE = external
        yield external
        return

    try:
        # pyads 未提供 py.typed/stubs，属可选的 ads extra，局部豁免未类型化导入
        from pyads.constants import ADST_REAL32  # type: ignore[import-untyped]
        from pyads.testserver import (  # type: ignore[import-untyped]
            AdsTestServer,
            AdvancedHandler,
            PLCVariable,
        )
    except ImportError:
        pytest.skip(
            "SKIPPED: real ADS environment not configured and pyads "
            "testserver unavailable (install the 'ads' extra)"
        )

    handler = AdvancedHandler()
    read_variable = PLCVariable(
        _TESTSERVER_READ_SYMBOL,
        21.5,
        ads_type=ADST_REAL32,
        symbol_type="REAL",
    )
    write_variable = PLCVariable(
        _TESTSERVER_WRITE_SYMBOL,
        0.0,
        ads_type=ADST_REAL32,
        symbol_type="REAL",
    )
    handler.add_variable(read_variable)
    handler.add_variable(write_variable)
    server = AdsTestServer(
        handler=handler,
        ip_address="127.0.0.1",
        port=_ADS_TCP_PORT,
        logging=False,
    )
    server.start()
    _wait_tcp_port("127.0.0.1", _ADS_TCP_PORT)
    _ADS_SERVICE_CACHE = {
        "host": "127.0.0.1",
        "net_id": _TESTSERVER_NET_ID,
        "port": _ADS_TCP_PORT,
        "read_symbol": _TESTSERVER_READ_SYMBOL,
        "write_symbol": _TESTSERVER_WRITE_SYMBOL,
        "local_net_id": _TESTSERVER_LOCAL_NET_ID,
        "local_ip": "127.0.0.1",
        "backend": "testserver",
        "server": server,
        "handler": handler,
        "variables": {
            _TESTSERVER_READ_SYMBOL: read_variable,
            _TESTSERVER_WRITE_SYMBOL: write_variable,
        },
    }
    try:
        yield _ADS_SERVICE_CACHE
    finally:
        # 多个 fixture 实例共享缓存；只停一次（可靠性测试重建过的 server 也在缓存里）。
        if _ADS_SERVICE_CACHE is not None:
            try:
                _ADS_SERVICE_CACHE["server"].stop()
            finally:
                _ADS_SERVICE_CACHE = None
