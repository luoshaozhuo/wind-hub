"""网络诊断 network.py 契约。

- 输出解析：ping（Linux/macOS 两种 RTT 行）、ip -j JSON、ss 列。
- 真实受控 loopback：TCP 探测成功/失败、端口扫描去重与数量。
- 边界：count/timeout/port/concurrency/扫描数量。
- 子进程：超时 kill+回收、取消回收；参数数组执行（无 shell 拼接）。
- iperf3：失败与异常输出一律 UNKNOWN。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.infrastructure.diagnostic import network


class _CommandResult:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.code = code
        self.out = out
        self.err = err


@pytest.fixture
def fake_command(monkeypatch: pytest.MonkeyPatch):
    """按可调用对象接管 _command，记录全部 argv。"""
    calls: list[tuple[str, ...]] = []
    result = _CommandResult(0)

    async def _fake(*argv: str, timeout: float = 3.0) -> tuple[int, str, str]:
        calls.append(argv)
        return result.code, result.out, result.err

    monkeypatch.setattr(network, "_command", _fake)
    return calls, result


# --------------------------------------------------------------------------
# Ping
# --------------------------------------------------------------------------

_LINUX_RTT = (
    "PING host (10.0.0.1) 56(84) bytes of data.\n"
    "64 bytes: icmp_seq=1 ttl=64\n"
    "--- host ping statistics ---\n"
    "rtt min/avg/max/mdev = 0.123/0.234/0.456/0.078 ms\n"
)
_MAC_RTT = "round-trip min/avg/max/stddev = 1.1/2.2/3.3/0.4 ms\n"


@pytest.mark.parametrize("rtt_line", [_LINUX_RTT, _MAC_RTT])
async def test_ping_probe_parses_rtt_statistics(fake_command, rtt_line: str) -> None:
    calls, result = fake_command
    result.code, result.out = 0, rtt_line

    probe = await network.ping_probe("10.0.0.1", count=4, timeout=1.0)

    assert probe.status is DiagnosticStatus.PASS
    assert probe.findings[0].code == "PING_OK"
    assert probe.measurements["rtt_avg_ms"] > 0
    assert "rtt_deviation_ms" in probe.measurements
    # argv 数组形式：host 永远是独立的最后一个参数，不存在 shell 拼接。
    assert calls[0][-1] == "10.0.0.1"


async def test_ping_failure_does_not_infer_firewall(fake_command) -> None:
    _, result = fake_command
    result.code, result.out, result.err = 1, "", "100% packet loss"

    probe = await network.ping_probe("10.0.0.2")

    assert probe.status is DiagnosticStatus.FAIL
    assert probe.findings[0].code == "PING_FAILED"
    assert "TCP may still work" in probe.findings[0].message


async def test_ping_without_rtt_line_has_no_timings(fake_command) -> None:
    _, result = fake_command
    result.code, result.out = 0, "ping ok but no summary\n"

    probe = await network.ping_probe("host")

    assert probe.status is DiagnosticStatus.PASS
    assert probe.measurements == {}


async def test_ping_unavailable_binary_is_unknown(fake_command) -> None:
    _, result = fake_command
    result.code, result.err = 127, "FileNotFoundError: [Errno 2]"

    probe = await network.ping_probe("host")

    assert probe.status is DiagnosticStatus.UNKNOWN
    assert probe.findings[0].code == "PING_UNAVAILABLE"


@pytest.mark.parametrize("count", [0, 21])
async def test_ping_count_bounds(fake_command, count: int) -> None:
    with pytest.raises(ValueError):
        await network.ping_probe("h", count=count)


@pytest.mark.parametrize("timeout", [0, -1, 31])
async def test_ping_timeout_bounds(fake_command, timeout: float) -> None:
    with pytest.raises(ValueError):
        await network.ping_probe("h", timeout=timeout)


async def test_ping_host_returns_bool(fake_command) -> None:
    _, result = fake_command
    result.code = 0
    assert await network.ping_host("h") is True
    result.code = 1
    assert await network.ping_host("h") is False


# --------------------------------------------------------------------------
# TCP / 端口扫描：真实 loopback 服务
# --------------------------------------------------------------------------


async def _echo_server() -> AsyncIterator[tuple[str, int]]:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    try:
        sock = server.sockets[0]
        yield "127.0.0.1", sock.getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def echo_endpoint():
    async for item in _echo_server():
        yield item


async def test_tcp_probe_open_port_is_pass(echo_endpoint) -> None:
    host, port = echo_endpoint
    probe = await network.tcp_probe(host, port, timeout=1.0)

    assert probe.status is DiagnosticStatus.PASS
    assert probe.findings[0].code == "TCP_CONNECT_OK"
    assert probe.measurements["connect_ms"] >= 0


async def test_tcp_probe_closed_port_is_fail() -> None:
    probe = await network.tcp_probe("127.0.0.1", 1, timeout=0.5)

    assert probe.status is DiagnosticStatus.FAIL
    assert probe.findings[0].code == "TCP_CONNECT_FAILED"
    assert "connect_ms" in probe.measurements


async def test_tcp_probe_closes_writer(echo_endpoint) -> None:
    # 正常路径不应抛异常（wait_closed 被执行）；重复探测同一端口验证资源释放。
    host, port = echo_endpoint
    for _ in range(3):
        probe = await network.tcp_probe(host, port)
        assert probe.status is DiagnosticStatus.PASS


@pytest.mark.parametrize("port", [0, 65536, -1])
async def test_tcp_probe_port_bounds(port: int) -> None:
    with pytest.raises(ValueError):
        await network.tcp_probe("h", port)


async def test_scan_ports_deduplicates_and_counts(echo_endpoint) -> None:
    host, open_port = echo_endpoint
    result = await network.scan_ports(
        host, [1, 1, open_port, open_port], timeout=0.5, concurrency=4
    )

    assert result.status is DiagnosticStatus.PASS
    assert result.measurements["probed"] == 2
    assert result.measurements["reachable"] == 1
    ports = {item["port"] for item in result.evidence["ports"]}  # type: ignore[index]
    assert ports == {1, open_port}


async def test_scan_concurrency_is_bounded(echo_endpoint, monkeypatch) -> None:
    host, open_port = echo_endpoint
    real_probe = network.tcp_probe
    active = 0
    peak = 0

    async def counted_probe(h: str, port: int, *, timeout: float = 1.0):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return await real_probe(h, port, timeout=timeout)

    monkeypatch.setattr(network, "tcp_probe", counted_probe)
    await network.scan_ports(host, [open_port] * 8, timeout=1.0, concurrency=2)

    assert peak <= 2


@pytest.mark.parametrize(
    "ports",
    [[], [0], [65536], list(range(1, 130))],
)
async def test_scan_rejects_invalid_port_sets(ports: Sequence[int]) -> None:
    with pytest.raises(ValueError):
        await network.scan_ports("h", ports)


@pytest.mark.parametrize("concurrency", [0, 17])
async def test_scan_concurrency_bounds(concurrency: int) -> None:
    with pytest.raises(ValueError):
        await network.scan_ports("h", [80], concurrency=concurrency)


# --------------------------------------------------------------------------
# ip / ss 解析
# --------------------------------------------------------------------------


async def test_interfaces_parses_ip_json(fake_command) -> None:
    _, result = fake_command
    result.out = '[{"ifindex": 1, "ifname": "lo"}]'

    probe = await network.interfaces()

    assert probe.status is DiagnosticStatus.PASS
    assert probe.findings[0].code == "IP_QUERY_OK"
    assert probe.evidence["items"][0]["ifname"] == "lo"  # type: ignore[index]


async def test_ip_command_failure_is_unknown(fake_command) -> None:
    _, result = fake_command
    result.code, result.err = 1, "ip: command failed"

    assert (await network.routes()).findings[0].code == "IP_QUERY_FAILED"


async def test_ip_invalid_json_is_unknown(fake_command) -> None:
    _, result = fake_command
    result.out = "not json"

    assert (await network.interfaces()).findings[0].code == "IP_INVALID_OUTPUT"


_SS_LINE = "tcp LISTEN 0 128 0.0.0.0:502 0.0.0.0:* " 'users:(("python3",pid=1234,fd=7))'
_SS_ESTAB = "tcp ESTAB 0 0 127.0.0.1:502 127.0.0.1:43210 " 'users:(("python3",pid=1234,fd=8))'


async def test_ports_parses_ss_rows_and_applies_filters(fake_command) -> None:
    _, result = fake_command
    result.out = f"{_SS_LINE}\n{_SS_ESTAB}\nudp UNCONN 0 0 0.0.0.0:68 0.0.0.0:*\n"

    all_sockets = await network.ports()
    assert all_sockets.measurements["count"] == 3

    listening = await network.ports(listening=True)
    assert {row["state"] for row in listening.evidence["sockets"]} <= {"LISTEN", "UNCONN"}  # type: ignore[index]

    established = await network.ports(established=True)
    assert all(row["state"] == "ESTAB" for row in established.evidence["sockets"])  # type: ignore[index]

    filtered = await network.ports(port=502)
    # LISTEN 与 ESTAB 的本地端都是 502，过滤器按本地端口数字匹配，两者都保留。
    assert filtered.measurements["count"] == 2
    assert filtered.evidence["sockets"][0]["local"].endswith(":502")  # type: ignore[index]
    assert "python3" in filtered.evidence["sockets"][0]["process"]  # type: ignore[index]


async def test_ports_invalid_port_raises() -> None:
    with pytest.raises(ValueError):
        await network.ports(port=0)


async def test_ss_failure_is_unknown(fake_command) -> None:
    _, result = fake_command
    result.code = 127
    probe = await network.ports()
    assert probe.status is DiagnosticStatus.UNKNOWN
    assert probe.findings[0].code == "SOCKET_QUERY_FAILED"


# --------------------------------------------------------------------------
# iperf3
# --------------------------------------------------------------------------


async def test_bandwidth_success_parses_throughput(fake_command) -> None:
    _, result = fake_command
    result.out = '{"end": {"sum_sent": {"bits_per_second": 9500000.0}}}'

    probe = await network.bandwidth("10.0.0.1", duration=3, port=5201)

    assert probe.status is DiagnosticStatus.PASS
    assert probe.findings[0].code == "IPERF3_COMPLETE"
    assert probe.measurements["sent_mbps"] == pytest.approx(9.5)


async def test_bandwidth_failure_is_unknown(fake_command) -> None:
    _, result = fake_command
    result.code, result.err = 1, "connection refused"

    probe = await network.bandwidth("10.0.0.1")

    assert probe.status is DiagnosticStatus.UNKNOWN
    assert probe.findings[0].code == "IPERF3_FAILED"


async def test_bandwidth_invalid_json_is_unknown(fake_command) -> None:
    _, result = fake_command
    result.out = '{"unexpected": true}'

    probe = await network.bandwidth("10.0.0.1")

    assert probe.findings[0].code == "IPERF3_INVALID_OUTPUT"


@pytest.mark.parametrize("duration", [0, 11])
async def test_bandwidth_duration_bounds(duration: int) -> None:
    with pytest.raises(ValueError):
        await network.bandwidth("h", duration=duration)


# --------------------------------------------------------------------------
# 子进程超时/取消回收与无 shell 拼接
# --------------------------------------------------------------------------

_MARKER = "windhubdiagmarker_7c3f"
_SLEEPER_CODE = (
    "import time,sys; " f"sys.stdout.write('{_MARKER}'); sys.stdout.flush(); time.sleep(30)"
)


async def _sleeper_pids() -> list[int]:
    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        "-f",
        _MARKER,
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return [int(line) for line in out.split()]


async def test_command_timeout_kills_and_reaps_child() -> None:
    task = asyncio.create_task(network._command(sys.executable, "-c", _SLEEPER_CODE, timeout=0.3))
    # 等待子进程真正启动，避免在 exec 之前就检查存活。
    for _ in range(20):
        if await _sleeper_pids():
            break
        await asyncio.sleep(0.02)
    assert await _sleeper_pids()

    done = await asyncio.wait_for(task, timeout=3)

    # 超时契约：返回 124 而非抛出；kill + communicate 完成后无残留子进程。
    assert done[0] == 124
    assert not await _sleeper_pids()


async def test_command_cancellation_reaps_child() -> None:
    task = asyncio.create_task(network._command(sys.executable, "-c", _SLEEPER_CODE, timeout=5))
    for _ in range(20):
        if await _sleeper_pids():
            break
        await asyncio.sleep(0.02)
    assert await _sleeper_pids()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not await _sleeper_pids()


async def test_command_missing_binary_returns_127() -> None:
    code, _, err = await network._command("/nonexistent/wind-hub-probe-bin")
    assert code == 127
    assert "FileNotFoundError" in err


async def test_no_shell_injection_through_host_argument(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned"
    malicious = f"127.0.0.1; touch {sentinel}"

    code, _, _ = await network._command("true", malicious, timeout=2)

    assert code == 0
    assert not sentinel.exists()


# --------------------------------------------------------------------------
# 便利封装与畸形行
# --------------------------------------------------------------------------


async def test_tcp_port_open_bool_wrapper(echo_endpoint) -> None:
    host, port = echo_endpoint
    assert await network.tcp_port_open(host, port) is True
    assert await network.tcp_port_open("127.0.0.1", 1, timeout=0.3) is False


async def test_ss_malformed_short_lines_are_skipped(fake_command) -> None:
    _, result = fake_command
    result.out = "garbage one-two\n" + _SS_LINE + "\n"

    probe = await network.ports()

    assert probe.measurements["count"] == 1
