"""UC-15 三种本地网络探测 System Test（真实子进程，真实本机网络栈）。"""

from __future__ import annotations

import socket

import pytest

pytestmark = [pytest.mark.system, pytest.mark.integration]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_net_ping_localhost_success(cli_env: object) -> None:
    """S01：ICMP ping 本机（rootless 下实现可回退）→ SUCCESS/0。"""
    result = cli_env.subprocess_invoke("net", "ping", "127.0.0.1")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["status"] == "UP"


def test_net_tcp_open_and_closed(cli_env: object) -> None:
    """S01/S03：开放端口 OPEN/0；关闭端口 UNKNOWN/3，无异常栈。"""
    port = _free_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    try:
        opened = cli_env.subprocess_invoke("net", "tcp", "127.0.0.1", str(port))
        assert opened.exit_code == 0, opened.stdout
        assert opened.json()["data"]["tcp_state"] == "OPEN"
    finally:
        listener.close()
    closed = cli_env.subprocess_invoke("net", "tcp", "127.0.0.1", str(port))
    assert closed.exit_code == 3, closed.stdout
    assert closed.json()["status"] == "UNKNOWN"


def test_net_ping_bad_arguments_exit2(cli_env: object) -> None:
    """S02：非法主机参数 → 2。"""
    result = cli_env.subprocess_invoke("net", "ping", "not-a-host!!")
    assert result.exit_code == 2, result.stdout


def test_net_industrial_all_ports_closed_is_success(cli_env: object) -> None:
    """UC-15：工业端口普查返回逐端口判定；本机端口全闭时诚实 CLOSED/TIMESTAMP 而非失败。"""
    result = cli_env.subprocess_invoke("net", "industrial", "127.0.0.1", "--timeout", "0.2")
    assert result.exit_code == 0, result.stdout
    data = result.json()["data"]
    assert len(data) >= 6
    assert {item["port"] for item in data}
    assert all(item["tcp_state"] in {"CLOSED", "TIMEOUT"} for item in data)


def test_net_scan_loopback_and_bad_cidr(cli_env: object) -> None:
    """UC-15：/30 快速扫描发现本机 UP；非法 CIDR 是参数错误 2。"""
    result = cli_env.subprocess_invoke("net", "scan", "127.0.0.0/30", "--timeout", "0.3")
    assert result.exit_code == 0, result.stdout
    data = result.json()["data"]
    assert data["scanned"] == 2
    assert data["reachable"] >= 1
    assert any(item["status"] == "UP" for item in data["results"])

    bad = cli_env.subprocess_invoke("net", "scan", "not-a-cidr")
    assert bad.exit_code == 2, bad.stdout
