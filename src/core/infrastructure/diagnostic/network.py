"""Linux 网络诊断：只读本机状态和受限主动连通性探测。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Sequence

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus


async def _command(*argv: str, timeout: float = 3.0) -> tuple[int, str, str]:
    """以参数数组执行系统命令，禁止 shell 拼接。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "", "command timed out"
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _result(
    target: str,
    status: DiagnosticStatus,
    code: str,
    message: str,
    *,
    evidence: dict[str, object] | None = None,
    measurements: dict[str, int | float | str] | None = None,
) -> DiagnosticResult:
    return DiagnosticResult(
        category="network",
        target=target,
        status=status,
        findings=(DiagnosticFinding(code=code, status=status, message=message),),
        evidence=evidence or {},
        measurements=measurements or {},
    )


async def ping_host(host: str, *, timeout: float = 1.0) -> bool:
    """基础 Ping；ICMP 禁用时失败不代表 TCP 不可用。"""
    code, _, _ = await _command(
        "ping", "-n", "-c", "1", "-W", str(max(1, int(timeout))), host,
        timeout=timeout + 1,
    )
    return code == 0


async def tcp_port_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    return (await tcp_probe(host, port, timeout=timeout)).status is DiagnosticStatus.PASS


async def ping_probe(host: str, *, count: int = 4, timeout: float = 1.0) -> DiagnosticResult:
    """Ping 与往返时延原始统计；不从 ICMP 失败推断防火墙状态。"""
    if not 1 <= count <= 20 or not 0 < timeout <= 30:
        raise ValueError("count or timeout is out of range")
    code, out, err = await _command(
        "ping", "-n", "-c", str(count), "-W", str(max(1, int(timeout))), host,
        timeout=count * (timeout + 1) + 2,
    )
    if code == 127:
        return _result(host, DiagnosticStatus.UNKNOWN, "PING_UNAVAILABLE", err)
    return _result(
        host,
        DiagnosticStatus.PASS if code == 0 else DiagnosticStatus.FAIL,
        "PING_OK" if code == 0 else "PING_FAILED",
        "ICMP probe completed" if code == 0 else "ICMP probe failed; TCP may still work",
        evidence={"output": out[-4000:], "error": err[-500:]},
    )


async def tcp_probe(host: str, port: int, *, timeout: float = 1.0) -> DiagnosticResult:
    if not 1 <= port <= 65535 or not 0 < timeout <= 30:
        raise ValueError("invalid TCP port or timeout")
    started = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return _result(
            f"{host}:{port}", DiagnosticStatus.FAIL, "TCP_CONNECT_FAILED",
            f"{type(exc).__name__}: {exc}",
            measurements={"connect_ms": round(elapsed, 3)},
        )
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout)
    return _result(
        f"{host}:{port}", DiagnosticStatus.PASS, "TCP_CONNECT_OK",
        "TCP connection established; application protocol not verified",
        measurements={"connect_ms": round((time.perf_counter() - started) * 1000, 3)},
    )


async def scan_ports(
    host: str,
    ports: Sequence[int],
    *,
    timeout: float = 1.0,
    concurrency: int = 8,
) -> DiagnosticResult:
    """仅扫描显式给定的少量端口；不扫描整个网段。"""
    unique = sorted(set(ports))
    if not unique or len(unique) > 128 or any(not 1 <= p <= 65535 for p in unique):
        raise ValueError("scan accepts 1..128 valid ports")
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be 1..16")
    limit = asyncio.Semaphore(concurrency)

    async def check(port: int) -> dict[str, object]:
        async with limit:
            result = await tcp_probe(host, port, timeout=timeout)
            return {"port": port, "status": result.status.value, **result.measurements}

    observations = await asyncio.gather(*(check(p) for p in unique))
    open_ports = sum(item["status"] == "PASS" for item in observations)
    return _result(
        host, DiagnosticStatus.PASS, "PORT_SCAN_COMPLETE",
        "Port scan completed; filtered and closed causes are not distinguished",
        evidence={"ports": observations},
        measurements={"probed": len(unique), "reachable": open_ports},
    )


async def _json_ip(*args: str) -> DiagnosticResult:
    code, out, err = await _command("ip", "-j", *args)
    target = " ".join(args)
    if code != 0:
        return _result(target, DiagnosticStatus.UNKNOWN, "IP_QUERY_FAILED", err)
    try:
        payload = json.loads(out)
    except ValueError:
        return _result(target, DiagnosticStatus.UNKNOWN, "IP_INVALID_OUTPUT", "Invalid ip JSON")
    return _result(
        target, DiagnosticStatus.PASS, "IP_QUERY_OK", "Local kernel network state",
        evidence={"items": payload},
    )


async def interfaces() -> DiagnosticResult:
    return await _json_ip("address", "show")


async def routes() -> DiagnosticResult:
    return await _json_ip("route", "show", "table", "all")


async def ports(
    *,
    listening: bool = False,
    established: bool = False,
    port: int | None = None,
) -> DiagnosticResult:
    """使用 ss 查看 TCP/UDP Socket；PID 可能因权限不足而缺失。"""
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid port")
    args = ["ss", "-H", "-n", "-a", "-t", "-u", "-p"]
    code, out, err = await _command(*args)
    if code != 0:
        return _result("localhost", DiagnosticStatus.UNKNOWN, "SOCKET_QUERY_FAILED", err)
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        columns = line.split(maxsplit=6)
        if len(columns) < 6:
            continue
        state, recv_q, send_q, local, peer = columns[1:6]
        protocol = columns[0]
        if listening and state not in {"LISTEN", "UNCONN"}:
            continue
        if established and state != "ESTAB":
            continue
        if port is not None and local.rsplit(":", 1)[-1] != str(port):
            continue
        rows.append({
            "protocol": protocol,
            "state": state,
            "local": local,
            "peer": peer,
            "recv_q": recv_q,
            "send_q": send_q,
            "process": columns[6] if len(columns) > 6 else "",
        })
    return _result(
        "localhost", DiagnosticStatus.PASS, "SOCKET_QUERY_OK",
        "Socket snapshot; PID/process visibility depends on privileges",
        evidence={"sockets": rows},
        measurements={"count": len(rows)},
    )
