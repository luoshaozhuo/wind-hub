"""pyads AdsTestServer 启停与同端口重建 helper（可靠性测试共用）。

AdsTestServer 是真实 AMS/TCP 协议栈（``real_service``）。``stop()`` 关闭监听
socket 与内核端口回收存在竞争，因此重建需要等端口释放并对 bind 做重试。
"""

from __future__ import annotations

import socket
import time
from typing import Any

TESTSERVER_READ_SYMBOL = "Main.temperature"
TESTSERVER_WRITE_SYMBOL = "Main.setpoint"
TESTSERVER_NET_ID = "127.0.0.1.2.1"
TESTSERVER_LOCAL_NET_ID = "127.0.0.1.1.1"
ADS_TCP_PORT = 48898


def wait_tcp_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"ADS testserver did not open {host}:{port} within {timeout}s")


def wait_port_closed(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                time.sleep(0.05)
        except OSError:
            return
    raise RuntimeError(f"ADS testserver port {host}:{port} did not release")


def start_testserver(ads: dict[str, Any]) -> Any:
    """在 ads 字典描述的端口上新建 AdsTestServer，并原地写入 server/variables。"""
    from pyads.constants import ADST_REAL32  # type: ignore[import-untyped]
    from pyads.testserver import (  # type: ignore[import-untyped]
        AdsTestServer,
        AdvancedHandler,
        PLCVariable,
    )

    host = str(ads["host"])
    port = int(ads["port"])
    handler = AdvancedHandler()
    read_variable = PLCVariable(
        str(ads["read_symbol"]), 21.5, ads_type=ADST_REAL32, symbol_type="REAL"
    )
    write_variable = PLCVariable(
        str(ads["write_symbol"]), 0.0, ads_type=ADST_REAL32, symbol_type="REAL"
    )
    handler.add_variable(read_variable)
    handler.add_variable(write_variable)
    deadline = time.monotonic() + 5.0
    while True:
        try:
            server = AdsTestServer(handler=handler, ip_address=host, port=port, logging=False)
            server.start()
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    wait_tcp_port(host, port)
    ads["server"] = server
    ads["handler"] = handler
    ads["variables"] = {
        str(ads["read_symbol"]): read_variable,
        str(ads["write_symbol"]): write_variable,
    }
    return server


def restart_testserver(ads: dict[str, Any]) -> Any:
    ads["server"].stop()
    wait_port_closed(str(ads["host"]), int(ads["port"]))
    return start_testserver(ads)
