"""System E2E 的进程管理 harness——服务进程一律以真实 subprocess 运行。

本模块是 tests/system 的唯一服务进程入口，禁止在测试里直接拼
``subprocess.Popen``：

- Collector / Commander / Web Server 经各自 console script 启动（与生产
  一致），stdout/stderr 合并落盘到临时日志文件，断言失败时可直接贴日志；
- 服务状态与任务控制一律经 Collector gRPC 接口（``tests.support.control``），
  不再有 CLI 控制面；
- 所有等待都是带超时的轮询，不用硬 ``sleep``。

进程组语义：Collector 以独立进程组启动（``start_new_session``），
:meth:`CollectorProcess.terminate` 默认只向主进程发信号（与生产
SIGTERM 路径一致）；``kill_tree`` 用于测试失败后的强制清理。
"""

from __future__ import annotations

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


def free_port_block(
    count: int,
    *,
    exclude: set[int] | None = None,
) -> int:
    """寻找当前全空闲的连续本地端口段。

    使用临时 bind 验证全部端口，返回前关闭句柄；仍存在与其他进程
    的理论竞态，但失败将由真实协议服务 bind 报错，而非被悄悄跳过。
    """
    if not 1 <= count <= 100:
        raise ValueError("count must be within 1..100")
    excluded = exclude or set()
    for _ in range(100):
        candidate = free_port()
        if candidate + count > 65535:
            continue
        ports = set(range(candidate, candidate + count))
        if ports & excluded:
            continue
        opened: list[socket.socket] = []
        try:
            for port in sorted(ports):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                opened.append(sock)
                sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            for sock in opened:
                sock.close()
        return candidate
    raise RuntimeError(f"could not reserve a free range of {count} TCP ports")


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
            raise TimeoutError(f"collector did not exit within {timeout}s after SIGTERM") from None

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

    def fd_count(self) -> int:
        """主进程当前打开的 FD 数（/proc 采样，含 socket/文件/管道）。

        短周期故障测试的资源泄漏观测点：reconnect/reload 循环前后对比，
        单调增长即句柄泄漏。仅 Linux（测试执行环境）可用。
        """
        return len(os.listdir(f"/proc/{self.proc.pid}/fd"))

    def memory_rss_kb(self) -> int:
        """主进程常驻内存（KB，/proc/<pid>/statm 第 2 列 × 页大小）。

        稳定性测试的内存泄漏观测点：循环前后对比允许小幅波动，持续
        增长即泄漏。仅 Linux（测试执行环境）可用。
        """
        pages = int(Path(f"/proc/{self.proc.pid}/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // 1024

    def cpu_seconds(self) -> float:
        """主进程累计 CPU 时间（秒，utime+stime，/proc/<pid>/stat）。

        稳定性测试的 CPU 观测点：配合墙钟时间计算平均占用率，循环前后
        对比判断无失控循环。仅 Linux（测试执行环境）可用。
        """
        fields = Path(f"/proc/{self.proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])  # utime, stime（自 state 字段后起算）
        return ticks / os.sysconf("SC_CLK_TCK")


def start_collector(
    config_tree: Path,
    *,
    grpc_port: int | None = None,
    collector_id: str = "system-test",
    shutdown_timeout: float = 15.0,
    log_dir: Path,
    env_extra: dict[str, str] | None = None,
    admin_token: str | None = "system-test-admin-token",
) -> CollectorProcess:
    """以 subprocess 启动真实 ``wind-hub-collector``（v1 快照契约 CLI）。

    Args:
        config_tree: 配置树根，须含 ``services/service_collector-realtime_v1.1.0.yaml``、
            ``instances/``、``templates/``（见 tests.support.collector_config_tree）；
            v1 CLI 只接收服务文件，其余根目录按相对布局推导。
        grpc_port: gRPC 端口；None 时自动分配空闲端口。
        collector_id: Collector 标识。
        shutdown_timeout: 传入进程的优雅停机硬超时（秒）。
        log_dir: 进程日志目录（通常 ``tmp_path``）；state-dir 置于其下，
            同工厂的重启用例因此共享不可变配置代际与持久化意图。
        env_extra: 追加环境变量（叠加在当前进程环境上）。
        admin_token: 注入 WIND_HUB_COLLECTOR_ADMIN_TOKEN；None 时不注入
            （用于 fail-closed 负向测试）。对端 Web Server 必须注入同值。

    Returns:
        CollectorProcess（调用方负责后续 wait_grpc_ready 与 terminate）。
    """
    service_file = config_tree / "services" / "service_collector-realtime_v1.1.0.yaml"
    if not service_file.is_file():
        raise FileNotFoundError(
            f"{config_tree} is not a valid config tree: missing services/service_collector-realtime_v1.1.0.yaml"
        )
    env = dict(env_extra or {})
    if admin_token is not None:
        env.setdefault("WIND_HUB_COLLECTOR_ADMIN_TOKEN", admin_token)
    port = grpc_port if grpc_port is not None else free_port()
    return _start_service(
        "wind-hub-collector",
        [
            "--config",
            str(service_file),
            "--state-directory",
            str(log_dir / "collector-state"),
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
        env_extra=env,
    )


def start_commander(
    service_config: Path,
    *,
    grpc_port: int,
    log_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> CollectorProcess:
    """启动现行 Commander 子进程，仅传正式 --config 服务 YAML。

    gRPC 监听、证书身份和共享配置来源全部由服务 YAML 显式规定；
    grpc_port 与该文件配置必须一致，用于测试进程句柄的 endpoint。
    """
    if not service_config.is_file():
        raise FileNotFoundError(f"Commander service file missing: {service_config}")
    return _start_service(
        "wind-hub-commander",
        ["--config", str(service_config)],
        service="commander",
        port=grpc_port,
        log_dir=log_dir,
        env_extra=env_extra,
    )


def start_sink_service_from_root(
    config_root: Path,
    *,
    grpc_port: int,
    log_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> CollectorProcess:
    """Start the current three-process Sink Service from its canonical config root.

    The production entrypoint defaults to the shipped sink-west Service filename;
    synthetic test roots pin their own versioned identity, so the unique Service
    document in the tree is passed explicitly instead of relying on the default.
    """
    service_dir = config_root / "configs" / "services"
    service_files = tuple(service_dir.glob("service_*.yaml"))
    if len(service_files) != 1:
        raise FileNotFoundError(
            f"{config_root} must contain exactly one versioned Service file, "
            f"found {len(service_files)}"
        )
    service_file = service_files[0].relative_to(config_root).as_posix()
    return _start_service(
        "wind-hub-sink-service",
        ["--config-root", str(config_root), "--service-file", service_file],
        service="sink-service",
        port=grpc_port,
        log_dir=log_dir,
        env_extra=env_extra,
        cwd=config_root,
    )


def start_web_server(
    config_dir: Path,
    *,
    http_port: int,
    collectors: tuple[str, ...],
    commander: str,
    log_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> CollectorProcess:
    """启动真实 Web Server 进程，共享标准进程组、日志和清理入口。

    collectors 中每项为 worker_id=host:port；端口均由 E2E 自行分配。
    """
    if not collectors:
        raise ValueError("Web Server E2E requires at least one Collector")
    argv = [
        "--config",
        str(config_dir),
        "--host",
        "127.0.0.1",
        "--port",
        str(http_port),
        "--commander",
        commander,
        "--worker-probe-interval",
        "60",
    ]
    for collector in collectors:
        argv.extend(["--collector", collector])
    return _start_service(
        "wind-hub-server",
        argv,
        service="web-server",
        port=http_port,
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
    cwd: Path | None = None,
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
        cwd=cwd,
    )
    return CollectorProcess(
        proc=proc,
        grpc_target=f"127.0.0.1:{port}",
        log_path=log_path,
        _log_file=log_file,
    )
