"""UC-01 Shell System Test：真实 PTY 中的 CLI 交互 Shell 生命周期。

- S01：启动提示符、执行命令、exit 退出（真实子进程 + openpty 控制终端）；
- S02：历史持久化、JSON 信封、补全词表；
- S03：远端不可达不退出、Ctrl+C 取消在途 RPC 后 Shell 继续可用、通道随会话释放。
"""

from __future__ import annotations

import contextlib
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.integration]

SRC = Path(__file__).resolve().parents[2] / "src"


class PtyShell:
    def __init__(self, config: Path, cwd: Path = SRC) -> None:
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cli",
                "--config",
                str(config),
                "--format",
                "json",
            ],
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm", "COLUMNS": "200"},
        )
        os.close(slave)
        self.buffer = ""
        self.consumed = 0

    def read_for(self, needle: str, timeout: float = 8.0) -> str:
        """只在尚未消费的新输出中查找，避免旧提示符/回显造成假匹配。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                raw = os.read(self.master, 65536)
                if b"\x1b[6n" in raw:
                    # Answer prompt_toolkit's cursor-position request so it does
                    # not spend the CPR timeout gating the first submitted line.
                    os.write(self.master, b"\x1b[2;1R")
                self.buffer += raw.decode(errors="replace")
                if needle in self.buffer[self.consumed :]:
                    start = self.consumed
                    self.consumed = len(self.buffer)
                    return self.buffer[start:]
            if self.proc.poll() is not None and not ready:
                break
        raise AssertionError(
            f"did not see {needle!r}; tail={self.buffer[self.consumed:][-1000:]!r}"
        )

    def send(self, line: str, settle: float = 0.0) -> None:
        if settle:
            time.sleep(settle)
        os.write(self.master, (line + "\n").encode())

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.master)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _config(tmp_path: Path, certs: dict[str, Path], port: int) -> Path:
    cfg = tmp_path / "cli.yaml"
    services = "\n".join(
        f"""  {name}:
    grpc_endpoint: "localhost:{port}"
    tls_profile: mtls"""
        for name in (
            "collector",
            "commander",
            "sink_realtime",
            "sink_reliable",
            "gateway",
            "data_generator",
        )
    )
    cfg.write_text(
        f"""schema_version: 1
cli:
  timeout_seconds: 5
  output_format: json
services:
{services}
tls_profiles:
  mtls:
    ca_cert: {certs['ca']}
    client_cert: {certs['client_crt']}
    client_key: {certs['client_key']}
""",
        encoding="utf-8",
    )
    return cfg


def test_shell_start_command_exit(cli_env: Any, tmp_path: Any) -> None:
    """S01：提示符→真实 RPC 命令→exit 0；输出为 JSON 信封。"""
    cli_env.server.respond_value(
        cli_env.server.method_path("CommonService", "GetServiceInfo"),
        common_pb2.GetServiceInfoResponse(
            service_id="collector", software_version="1.0", contract_version="v1"
        ),
    )
    shell = PtyShell(_config(tmp_path, cli_env.server.cert_files, cli_env.server.port))
    try:
        shell.read_for("wind-hub>")
        shell.send("service info collector")
        output = shell.read_for('"command": "service info"')
        assert '"status": "SUCCESS"' in output
        shell.read_for("wind-hub>")
        shell.send("exit")
        time.sleep(1.0)
    finally:
        shell.close()
    assert shell.proc.returncode == 0


def test_shell_history_navigation_and_redaction(cli_env: Any, tmp_path: Any) -> None:
    """S02：上方向键回放会话历史；含 token 的命令不进入历史（再按上键不会回放它）。"""
    shell = PtyShell(_config(tmp_path, cli_env.server.cert_files, cli_env.server.port))
    try:
        shell.read_for("wind-hub>")
        shell.send("net ping 127.0.0.1")
        shell.read_for('"command": "net ping"')
        shell.read_for("wind-hub>")
        # 含 token 的敏感命令（多参数被 CLI 拒绝），无论是否执行都不得入历史
        shell.send("net ping 127.0.0.1 token=secret")
        shell.read_for("Got unexpected extra argument")
        shell.read_for("wind-hub>")
        # 上方向键 + 回车：回放的应是安全的上一条 ping，而不是带 token 的命令
        os.write(shell.master, b"\x1b[A\r")
        output = shell.read_for('"status": "SUCCESS"')
        assert "token" not in output
        shell.read_for("wind-hub>")
        shell.send("exit")
        time.sleep(1.0)
    finally:
        shell.close()
    assert shell.proc.returncode == 0


def test_shell_unreachable_keeps_session(cli_env: Any, tmp_path: Any) -> None:
    """S03：远端 UNIMPLEMENTED/不可达只产生 UNKNOWN 信封，Shell 不退出、可继续本地命令。"""
    cli_env.server.fail(
        cli_env.server.method_path("CommonService", "GetServiceInfo"),
        __import__("grpc").StatusCode.UNAVAILABLE,
        "down",
    )
    shell = PtyShell(_config(tmp_path, cli_env.server.cert_files, cli_env.server.port))
    try:
        shell.read_for("wind-hub>")
        shell.send("service info collector")
        output = shell.read_for('"status": "UNKNOWN"')
        assert '"error_code": "UNAVAILABLE"' in output
        shell.read_for("wind-hub>")
        # 会话仍然存活，本地命令继续可用
        shell.send("net ping 127.0.0.1")
        output = shell.read_for('"command": "net ping"')
        assert '"status": "SUCCESS"' in output
        shell.read_for("wind-hub>")
        shell.send("exit")
        time.sleep(1.0)
    finally:
        shell.close()
    assert shell.proc.returncode == 0


def test_shell_ctrl_c_cancels_inflight_rpc_and_continues(cli_env: Any, tmp_path: Any) -> None:
    """S03（CLI-P1-SHELL-01 复测）：真实 PTY + 真实在途慢 RPC。

    要求：Ctrl+C 后输出 INTERRUPTED 信封；服务端处理协程真实收到 CancelledError
    （而不是跑到 DEADLINE）；无重复分派、无孤儿 Task 异常；Shell 不退出，
    连续中断两次后仍可执行下一条命令。
    """
    import asyncio

    events: list[str] = []
    inflight = {"n": 0}

    async def slow(request: Any, context: Any) -> Any:
        inflight["n"] += 1
        seq = inflight["n"]
        events.append(f"start-{seq}")
        try:
            await asyncio.sleep(10)
            return common_pb2.GetServiceInfoResponse(service_id="x")
        except asyncio.CancelledError:
            events.append(f"cancelled-{seq}")
            raise

    cli_env.server.respond(cli_env.server.method_path("CommonService", "GetServiceInfo"), slow)
    shell = PtyShell(_config(tmp_path, cli_env.server.cert_files, cli_env.server.port))

    def interrupt_once(label: str) -> None:
        before = len(cli_env.server.calls)
        shell.send("service info collector")
        deadline = time.monotonic() + 5.0
        while f"start-{label}" not in events:
            if time.monotonic() > deadline:
                raise AssertionError(f"RPC {label} never went in flight: {events}")
            time.sleep(0.1)
        time.sleep(0.5)  # 让 RPC 稳定在途
        os.kill(shell.proc.pid, signal.SIGINT)
        output = shell.read_for('"status": "INTERRUPTED"', timeout=6.0)
        # 提示符可能与中断信封在同一个读出块中，容忍两种到达顺序。
        if "wind-hub>" not in output:
            shell.read_for("wind-hub>")
        assert "error_code" in output and "INTERRUPTED" in output
        assert "Task exception was never retrieved" not in output
        assert "Unhandled exception in event loop" not in output
        assert "Press ENTER to continue" not in output
        assert len(cli_env.server.calls) == before + 1  # 无重复分派
        # 客户端 INTERRUPTED 信封可能先于 RST_STREAM 到达服务端；等待服务端
        # 处理协程真实记账 CancelledError（而不是让它跑到 DEADLINE）。
        cancel_deadline = time.monotonic() + 5.0
        while f"cancelled-{label}" not in events and time.monotonic() < cancel_deadline:
            time.sleep(0.05)
        assert f"cancelled-{label}" in events, events

    try:
        shell.read_for("wind-hub>")
        interrupt_once(1)
        interrupt_once(2)
        assert events == ["start-1", "cancelled-1", "start-2", "cancelled-2"], events
        # Shell 未退出，继续执行本地命令；channel 与会话仍可用
        shell.send("net ping 127.0.0.1")
        output = shell.read_for('"command": "net ping"')
        assert '"status": "SUCCESS"' in output
        # 两次中断后 prompt_toolkit 的 CPR 重绘可能把提示符文字擦到滚屏区，
        # 不再逐字节匹配提示符；以 exit 被会话接受并干净退出 rc=0 证明存活。
        shell.send("exit")
        deadline = time.monotonic() + 5.0
        while shell.proc.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([shell.master], [], [], 0.2)
            if ready:
                try:
                    raw = os.read(shell.master, 65536)
                except OSError:
                    break  # 子进程已退出，PTY 对端关闭
                if b"\x1b[6n" in raw:
                    os.write(shell.master, b"\x1b[2;1R")
        assert shell.proc.poll() == 0, shell.proc.poll()
    finally:
        shell.close()
    assert shell.proc.returncode == 0
