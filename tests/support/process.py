"""System E2E 的进程管理 harness——Collector / ctl 一律以真实 subprocess 运行。

本模块是 tests/system 与 tests/recovery 的唯一进程入口，禁止在测试里
直接拼 ``subprocess.Popen``：

- Collector 经 console script ``wind-hub-collector`` 启动（与生产一致），
  stdout/stderr 合并落盘到临时日志文件，断言失败时可直接贴日志；
- ctl 经 console script ``wind-hub-ctl`` 执行，返回退出码 + stdout/stderr，
  JSON 输出由调用方 ``json.loads``；
- 所有等待都是带超时的轮询，不用硬 ``sleep``。

进程组语义：Collector 以独立进程组启动（``start_new_session``），
:meth:`CollectorProcess.terminate` 默认只向主进程发信号（与生产
SIGTERM 路径一致）；``kill_tree`` 用于测试失败后的强制清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def free_port() -> int:
    """取一个当前空闲的本地 TCP 端口（随后释放，调用方立即使用）。

    存在理论上的竞态（释放后被其他进程抢占）；测试全部绑定 127.0.0.1
    且立即使用，冲突概率可忽略，冲突时表现为明确的 bind 失败而非挂起。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class CtlResult:
    """一次 ``wind-hub-ctl`` 执行的结果。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """退出码为 0（RPC 失败时 ctl 约定返回 2）。"""
        return self.returncode == 0


def run_ctl(
    *args: str,
    target: str,
    rpc_timeout: float = 10.0,
    exec_timeout: float = 30.0,
) -> CtlResult:
    """以 subprocess 执行一次 ``wind-hub-ctl``。

    Args:
        args: 子命令及参数（如 ``("start", "task-1")``）。
        target: Collector gRPC endpoint（``host:port``）。
        rpc_timeout: ctl 单次 RPC 超时（秒）。
        exec_timeout: 子进程整体硬超时（秒）；超时杀进程并按失败返回。

    Returns:
        CtlResult；exec_timeout 触发时 returncode 为 -SIGKILL。
    """
    cmd = [
        "wind-hub-ctl",
        "--target",
        target,
        "--rpc-timeout",
        str(rpc_timeout),
        *args,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=exec_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CtlResult(
            returncode=-signal.SIGKILL,
            stdout=exc.stdout if isinstance(exc.stdout, str) else "",
            stderr=f"ctl exec timeout after {exec_timeout}s",
        )
    return CtlResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


async def run_ctl_async(*args: str, **kwargs: float | str) -> CtlResult:
    """:func:`run_ctl` 的异步包装——在独立线程执行阻塞 subprocess 调用。

    测试进程的事件循环同时承载协议 fixture server；同步调用会阻塞循环，
    导致 Collector 的读写请求在 ctl 执行期间全部超时。异步测试一律用本
    包装，不用裸 ``run_ctl``。
    """
    return await asyncio.to_thread(run_ctl, *args, **kwargs)


@dataclass
class CollectorProcess:
    """运行中的 Collector subprocess 句柄。

    Attributes:
        proc: 底层 Popen（独立进程组）。
        grpc_target: gRPC 控制面 endpoint。
        log_path: 合并后的 stdout/stderr 日志文件。
    """

    proc: subprocess.Popen[str]
    grpc_target: str
    log_path: Path
    _log_file: object = field(repr=False)

    def is_running(self) -> bool:
        """主进程仍在运行。"""
        return self.proc.poll() is None

    def read_log(self) -> str:
        """读取当前累计日志（断言失败诊断用）。"""
        self._log_file.flush()  # type: ignore[attr-defined]
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def terminate(self, *, timeout: float = 30.0) -> int:
        """发送 SIGTERM 并等待优雅退出。

        Returns:
            进程退出码。

        Raises:
            TimeoutError: 超时未退出（进程随后被 SIGKILL，避免残留）。
        """
        self.proc.send_signal(signal.SIGTERM)
        try:
            return int(self.proc.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            self.kill_tree()
            raise TimeoutError(
                f"collector did not exit within {timeout}s after SIGTERM"
            ) from None

    def kill_tree(self) -> None:
        """强制终止整个进程组（测试清理兜底，非优雅路径）。"""
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        # 进程组已 SIGKILL；wait 仅为回收僵尸，失败不掩盖测试结果
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=10)

    def close_log(self) -> None:
        """关闭日志文件句柄（进程退出后调用）。"""
        self._log_file.close()  # type: ignore[attr-defined]


def start_collector(
    config_dir: Path,
    *,
    grpc_port: int | None = None,
    collector_id: str = "system-test",
    shutdown_timeout: float = 15.0,
    log_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> CollectorProcess:
    """以 subprocess 启动真实 ``wind-hub-collector``。

    Args:
        config_dir: 现场配置目录。
        grpc_port: gRPC 端口；None 时自动分配空闲端口。
        collector_id: Collector 标识。
        shutdown_timeout: 传入进程的优雅停机硬超时（秒）。
        log_dir: 进程日志目录（通常 ``tmp_path``）。
        env_extra: 追加环境变量（叠加在当前进程环境上）。

    Returns:
        CollectorProcess（调用方负责后续 wait_grpc_ready 与 terminate）。
    """
    port = grpc_port if grpc_port is not None else free_port()
    return _start_service(
        "wind-hub-collector",
        [
            "--config",
            str(config_dir),
            "--collector-id",
            collector_id,
            "--grpc-host",
            "127.0.0.1",
            "--grpc-port",
            str(port),
            "--shutdown-timeout",
            str(shutdown_timeout),
        ],
        service="collector",
        port=port,
        log_dir=log_dir,
        env_extra=env_extra,
    )


def start_commander(
    config_dir: Path,
    *,
    grpc_port: int | None = None,
    log_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> CollectorProcess:
    """以 subprocess 启动真实 ``wind-hub-commander``。

    Args:
        config_dir: 现场配置目录（Commander 只读取 system/device_models/
            devices/points，忽略 tasks 与 sinks）。
        grpc_port: gRPC 端口；None 时自动分配空闲端口。
        log_dir: 进程日志目录（通常 ``tmp_path``）。
        env_extra: 追加环境变量（叠加在当前进程环境上）。

    Returns:
        CollectorProcess（通用进程句柄；调用方负责 wait_grpc_ready 与
        terminate）。
    """
    port = grpc_port if grpc_port is not None else free_port()
    return _start_service(
        "wind-hub-commander",
        [
            "--config",
            str(config_dir),
            "--grpc-host",
            "127.0.0.1",
            "--grpc-port",
            str(port),
        ],
        service="commander",
        port=port,
        log_dir=log_dir,
        env_extra=env_extra,
    )


def start_server(
    config_dir: Path,
    *,
    http_port: int | None = None,
    collectors: list[str],
    commander: str,
    log_dir: Path,
    reconcile_interval: float = 5.0,
    worker_probe_interval: float = 1.0,
    env_extra: dict[str, str] | None = None,
) -> CollectorProcess:
    """以 subprocess 启动真实 ``wind-hub-server``。

    Args:
        config_dir: 现场配置目录。
        http_port: REST API 端口；None 时自动分配空闲端口。
        collectors: ``worker_id=host:port`` 形式的 Collector 登记项。
        commander: Commander gRPC endpoint。
        log_dir: 进程日志目录（通常 ``tmp_path``）。
        reconcile_interval: Worker 配置对账周期（秒），测试默认缩短。
        worker_probe_interval: Worker 状态探测周期（秒），测试默认缩短。
        env_extra: 追加环境变量。

    Returns:
        CollectorProcess（通用进程句柄；``grpc_target`` 字段复用为
        ``127.0.0.1:<http_port>``，调用方负责 HTTP 就绪等待与 terminate）。
    """
    port = http_port if http_port is not None else free_port()
    args = [
        "--config",
        str(config_dir),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--commander",
        commander,
        "--reconcile-interval",
        str(reconcile_interval),
        "--worker-probe-interval",
        str(worker_probe_interval),
        "--log-level",
        "warning",
    ]
    for entry in collectors:
        args.extend(["--collector", entry])
    return _start_service(
        "wind-hub-server",
        args,
        service="server",
        port=port,
        log_dir=log_dir,
        env_extra=env_extra,
    )


def _start_service(
    script: str,
    args: list[str],
    *,
    service: str,
    port: int,
    log_dir: Path,
    env_extra: dict[str, str] | None,
) -> CollectorProcess:
    """以独立进程组启动 console script，stdout/stderr 合并落盘。"""
    log_path = log_dir / f"{service}-{port}.log"
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(  # noqa: S603
        [script, *args],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=env,
    )
    return CollectorProcess(
        proc=proc,
        grpc_target=f"127.0.0.1:{port}",
        log_path=log_path,
        _log_file=log_file,
    )
