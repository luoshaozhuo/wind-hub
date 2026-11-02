"""UC-02 真实 containerd 九宮格 System Test。

唯一允许的对端是本机真实 containerd 守护进程（native Tasks/Containers/Snapshots
gRPC，Unix socket）。不允许 mock daemon；socket/ctr 不可用时整模块 NOT_RUN（skip）。

默认对接仓库验收 dind（docker 容器 wh-ctr 内以 /tmp/wh-run/containerd.sock 提供
独立 root/state 的 containerd v1.7）。可用环境变量覆盖：

- ``WH_RUNTIME_SOCK``：containerd socket（默认 /tmp/wh-run/containerd.sock）
- ``WH_RUNTIME_CTR``：ctr 命令前缀（默认自动探测：本机 ctr 或 ``docker exec wh-ctr ctr``）

iSulad 九宫格本机无守护进程，见 test_system_real.test_isulad_or_not_run 的 NOT_RUN。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket as socket_mod
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import grpc
import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
SOCK = os.environ.get("WH_RUNTIME_SOCK", "/tmp/wh-run/containerd.sock")
IMAGE = "docker.io/library/busybox:1.36"
FIFO_ROOT = "/tmp/wh-run/e2e-fifo"
pytestmark = [pytest.mark.system, pytest.mark.real_service]


def _ctr_prefix() -> list[str] | None:
    override = os.environ.get("WH_RUNTIME_CTR")
    if override:
        return [*shlex.split(override), "--address", SOCK]
    ctr = shutil.which("ctr")
    if ctr:
        return [ctr, "--address", SOCK]
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "exec", "wh-ctr", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            return ["docker", "exec", "wh-ctr", "ctr", "--address", SOCK]
    return None


CTR = _ctr_prefix()


def _ctr(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    assert CTR is not None
    return subprocess.run(
        [*CTR, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _daemon_rm(path: str) -> None:
    """删除守护进程侧 root 所有物：dind 场景须在容器内执行 rm。"""
    if CTR and CTR[0] == "docker":
        subprocess.run(
            ["docker", "exec", "wh-ctr", "rm", "-rf", path],
            capture_output=True,
            timeout=30,
            check=False,
        )
    else:
        shutil.rmtree(path, ignore_errors=True)


def _daemon_alive() -> bool:
    if not CTR or not Path(SOCK).exists():
        return False
    version = _ctr("version", timeout=15)
    return version.returncode == 0


pytestmark = (
    pytestmark
    if _daemon_alive()
    else pytest.mark.skip(
        reason=f"NOT_RUN: 本机无可用 containerd（socket={SOCK}），UC-02 九宫格未执行；"
        "准备命令：docker run -d --name wh-ctr --privileged -v /tmp/wh-run:/tmp/wh-run "
        "docker:27-dind 并在其中以 state=/tmp/wh-run 启动 containerd + ctr image pull busybox:1.36"
    )
)


@pytest.fixture(scope="module", autouse=True)
def _image_present() -> None:
    listing = _ctr("images", "ls")
    assert listing.returncode == 0, listing.stderr
    if IMAGE not in listing.stdout:
        pulled = _ctr("images", "pull", IMAGE, timeout=180)
        if pulled.returncode != 0:
            pytest.skip(f"NOT_RUN: 离线环境无法拉取 {IMAGE}：{pulled.stderr}")


def _cleanup(name: str) -> None:
    _ctr("task", "kill", "-s", "SIGKILL", name)
    time.sleep(0.4)
    _ctr("task", "rm", name)
    _ctr("containers", "rm", name)
    _daemon_rm(f"{FIFO_ROOT}/{name}")


@pytest.fixture
def cid() -> str:
    name = f"whe2e{uuid.uuid4().hex[:8]}"
    yield name
    _cleanup(name)


def _run_task(name: str, *cmd: str, fifo: bool = False) -> None:
    args = ["run", "-d"]
    if fifo:
        fifo_dir = f"{FIFO_ROOT}/{name}"
        subprocess.run(["mkdir", "-p", fifo_dir], check=True)
        args.extend(["--fifo-dir", fifo_dir])
    else:
        args.append("--null-io")
    args.extend([IMAGE, name, *cmd])
    created = _ctr(*args)
    assert created.returncode == 0, created.stderr
    deadline = time.time() + 10
    while time.time() < deadline:
        if _task_state(name) == "RUNNING":
            return
        time.sleep(0.2)
    raise AssertionError(f"task {name} never reached RUNNING")


def _task_state(name: str) -> str | None:
    listing = _ctr("tasks", "list")
    assert listing.returncode == 0, listing.stderr
    for line in listing.stdout.splitlines()[1:]:
        columns = line.split()
        if columns and columns[0] == name:
            return columns[2]
    return None


def _write_config(tmp_path: Path, name: str, *, timeout: float = 10.0, socket: str = SOCK) -> Path:
    config = tmp_path / "cli.yaml"
    config.write_text(
        f"""schema_version: 1
cli:
  timeout_seconds: {timeout}
  output_format: json
services:
  rt:
    manager: containerd
    target: {name}
    namespace: default
    manager_socket: {socket}
""",
        encoding="utf-8",
    )
    return config


def _cli(config: Path, *args: str, timeout: int = 40) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cli", "--config", str(config), "--format", "json", *args],
        cwd=SRC,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines, f"no JSON envelope: {completed.stdout!r}{completed.stderr!r}"
    return json.loads(lines[-1])


# ---------------- S01 正常链路 ----------------


def test_s01_status_real_running(cid: str, tmp_path: Path) -> None:
    """状态查询：真实 Tasks/Get，PID 与守护进程上报一致。"""
    _run_task(cid, "sleep", "3000")
    result = _cli(_write_config(tmp_path, cid), "service", "status", "rt")
    # 无独立 gRPC 端点：进程状态真实可信，业务健康只能 UNKNOWN/3。
    assert result.returncode == 3, result.stdout
    process = _json(result)["data"][0]["process"]
    assert process["state"] == "RUNNING"
    assert process["pid"] > 0
    assert process["container_id"] == cid


def test_s01_stop_graceful_sigterm(cid: str, tmp_path: Path) -> None:
    """停止：SIGTERM 后确认 STOPPED，幂等返回，rc=0（stop 不做业务健康承诺）。"""
    _run_task(cid, "sh", "-c", 'trap "exit 0" TERM; sleep 3000 & wait $!')
    result = _cli(_write_config(tmp_path, cid), "service", "stop", "rt", "--yes")
    assert result.returncode == 0, result.stdout
    assert _json(result)["data"]["process"]["state"] == "STOPPED"
    assert _task_state(cid) == "STOPPED"

    again = _cli(_write_config(tmp_path, cid), "service", "stop", "rt", "--yes")
    assert again.returncode == 0, again.stdout
    assert _json(again)["data"]["process"]["already_stopped"] is True


def test_s01_restart_null_io_confirmed(cid: str, tmp_path: Path) -> None:
    """重启：preflight→stop 确认→Delete/Create/Start→RUNNING 确认，新 PID 生效。"""
    _run_task(cid, "sh", "-c", 'trap "exit 0" TERM; sleep 3000 & wait $!')
    before = _ctr("tasks", "list")
    old_pid = int(
        next(line for line in before.stdout.splitlines() if line.startswith(cid)).split()[1]
    )
    result = _cli(_write_config(tmp_path, cid, timeout=20), "service", "restart", "rt", "--yes")
    envelope = _json(result)
    process = envelope["data"]["process"]
    assert process["state"] == "RUNNING"
    assert process["confirmed"] is True
    assert [stage["operation"] for stage in process["stages"]] == [
        "preflight",
        "stop",
        "delete_create_start",
        "running",
    ]
    assert process["pid"] != old_pid
    assert _task_state(cid) == "RUNNING"


# ---------------- S02 重复/延迟就绪 ----------------


def test_s02_start_already_running_is_idempotent(cid: str, tmp_path: Path) -> None:
    """重复 start：已在 RUNNING 不重复下发 Start，already_running 显式可见。"""
    _run_task(cid, "sleep", "3000")
    result = _cli(_write_config(tmp_path, cid), "service", "start", "rt", "--yes")
    process = _json(result)["data"]["process"]
    assert process["already_running"] is True
    assert "start_pid" not in process


def test_s02_start_created_task_confirmed(cid: str, tmp_path: Path) -> None:
    """延迟就绪：CREATED 任务真实 Start 并复核 RUNNING（start_pid 回传）。"""
    from cli import containerd_restart as cr

    created = _ctr("containers", "create", IMAGE, cid, "sleep", "3000")
    assert created.returncode == 0, created.stderr

    async def create_stopped_task() -> None:
        md = (("containerd-namespace", "default"),)
        async with grpc.aio.insecure_channel(f"unix://{SOCK}") as channel:

            def unary(service: str, method: str, response_type: str) -> Any:
                return channel.unary_unary(
                    f"/containerd.services.{service}.v1.{service.capitalize()}/{method}",
                    request_serializer=lambda request: request.SerializeToString(),
                    response_deserializer=cr._MESSAGES[response_type].FromString,
                )

            container = (
                await unary("containers", "Get", "GetContainerResponse")(
                    cr._MESSAGES["GetContainerRequest"](id=cid), metadata=md
                )
            ).container
            rootfs = await unary("snapshots", "Mounts", "MountsResponse")(
                cr._MESSAGES["MountsRequest"](
                    snapshotter=container.snapshotter, key=container.snapshot_key
                ),
                metadata=md,
            )
            request = cr._MESSAGES["CreateTaskRequest"](container_id=cid)
            request.rootfs.extend(rootfs.mounts)
            await channel.unary_unary(
                "/containerd.services.tasks.v1.Tasks/Create",
                request_serializer=lambda req: req.SerializeToString(),
                response_deserializer=lambda value: value,
            )(request, metadata=md)

    import asyncio

    asyncio.run(create_stopped_task())
    assert _task_state(cid) == "CREATED"

    result = _cli(_write_config(tmp_path, cid), "service", "start", "rt", "--yes")
    process = _json(result)["data"]["process"]
    assert process["state"] == "RUNNING"
    assert process["start_pid"] > 0
    assert _task_state(cid) == "RUNNING"


# ---------------- S03 异常与安全语义 ----------------


def test_s03_unreachable_socket_exit3(cid: str, tmp_path: Path) -> None:
    """不可达 socket → UNKNOWN/3，无 Traceback，绝不伪称成功。"""
    config = _write_config(tmp_path, cid, socket="/tmp/wh-run/does-not-exist.sock")
    result = _cli(config, "service", "status", "rt")
    assert result.returncode == 3, result.stdout
    assert _json(result)["status"] == "UNKNOWN"
    assert "Traceback" not in result.stdout


def test_s03_permission_denied_socket_exit3(cid: str, tmp_path: Path) -> None:
    """无权限访问 socket（0600 且属主非当前用户）→ UNKNOWN/3 的诚实失败。"""
    if os.geteuid() == 0:
        pytest.skip("root bypasses Unix socket permissions")
    sock_path = tmp_path / "denied.sock"
    listener = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)
    os.chmod(sock_path, 0o000)
    try:
        config = _write_config(tmp_path, cid, socket=str(sock_path))
        result = _cli(config, "service", "status", "rt")
        assert result.returncode == 3, result.stdout
        assert _json(result)["status"] == "UNKNOWN"
    finally:
        listener.close()


def test_s03_stop_sigterm_timeout_no_sigkill(cid: str, tmp_path: Path) -> None:
    """进程忽略 SIGTERM：到总预算诚实报 DEADLINE/3，禁止 SIGKILL 升级，任务必须仍在。"""
    _run_task(cid, "sh", "-c", "trap '' TERM; sleep 3000 & wait $!")
    config = _write_config(tmp_path, cid, timeout=4)
    result = _cli(config, "service", "stop", "rt", "--yes", timeout=30)
    assert result.returncode == 3, result.stdout
    envelope = _json(result)
    assert envelope["error_code"] == "TimeoutError"
    assert "stop not confirmed" in envelope["message"]
    assert _task_state(cid) == "RUNNING"


def test_s03_restart_missing_fifo_fails_preflight(cid: str, tmp_path: Path) -> None:
    """预检发现原始 FIFO 消失：FAILURE/1，且 Kill 尚未发生——原任务必须仍 RUNNING。"""
    _run_task(
        cid,
        "sh",
        "-c",
        'trap "exit 0" TERM; sleep 3000 & wait $!',
        fifo=True,
    )
    _daemon_rm(f"{FIFO_ROOT}/{cid}")
    result = _cli(_write_config(tmp_path, cid, timeout=8), "service", "restart", "rt", "--yes")
    assert result.returncode == 1, result.stdout
    envelope = _json(result)
    assert envelope["status"] == "FAILURE"
    assert "stream is unavailable" in envelope["message"]
    assert _task_state(cid) == "RUNNING"


def test_s03_restart_real_fifo_task_refuses_before_delete(cid: str, tmp_path: Path) -> None:
    """CLI-P1-RUNTIME-01：真实 FIFO 任务 restart 必须在任何破坏性操作前安全拒绝。

    这是 fail-closed 验收，不是 FIFO restart 能力验收：CLI 不具备受管 stdio relay，
    必须 FAILURE/1 拒绝；原任务保持 RUNNING、PID 不变（证明 Kill/Delete/Create 均未
    发生，也没有自动重试）。FIFO 完整 restart 能力仍未实现，不在本测试声称范围内。
    """
    _run_task(
        cid,
        "sh",
        "-c",
        'trap "exit 0" TERM; sleep 3000 & wait $!',
        fifo=True,
    )
    before = _ctr("tasks", "list")
    old_pid = int(
        next(line for line in before.stdout.splitlines() if line.startswith(cid)).split()[1]
    )
    result = _cli(_write_config(tmp_path, cid, timeout=12), "service", "restart", "rt", "--yes")
    assert result.returncode == 1, result.stdout
    envelope = _json(result)
    assert envelope["status"] == "FAILURE"
    assert "managed relay" in envelope["message"]
    assert "refusing to delete the existing task" in envelope["message"]
    # 破坏性操作前拒绝：任务仍在、init PID 不变（Kill/Delete/Create 均未发生）。
    assert _task_state(cid) == "RUNNING"
    after = _ctr("tasks", "list")
    new_pid = int(
        next(line for line in after.stdout.splitlines() if line.startswith(cid)).split()[1]
    )
    assert new_pid == old_pid
    # 失败信封不得携带任何 restart 阶段记账（preflight 已拒绝，stop 未执行）。
    process = envelope["data"].get("process") if isinstance(envelope.get("data"), dict) else None
    assert not (process and process.get("stages")), envelope


# ---------- UC-02 Linux 服务进程：真实 systemd --user ----------

_USER_BUS = Path(f"/run/user/{os.getuid()}/bus")
_SYSTEMD_USER = (
    _USER_BUS.exists()
    and shutil.which("systemd-run") is not None
    and subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout.strip()
    in {"running", "degraded"}
)


@pytest.fixture
def user_unit() -> str:
    if not _SYSTEMD_USER:
        pytest.skip("NOT_RUN: 本机无 systemd --user 总线，UC-02 systemd 九宫格未执行")
    unit = f"whe2e-{uuid.uuid4().hex[:8]}.service"
    # 真实单元文件：stop 后仍可 start/restart（瞬态单元停止即被 GC，无法代表被托管服务）。
    units_dir = Path.home() / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True, exist_ok=True)
    (units_dir / unit).write_text(
        "[Unit]\nDescription=wind-hub CLI acceptance\n" "[Service]\nExecStart=/usr/bin/sleep 300\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    created = subprocess.run(
        ["systemctl", "--user", "start", unit],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    for _ in range(50):
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        if active == "active":
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"{unit} never activated")
    yield unit
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, check=False)
    (units_dir / unit).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True, check=False)


def _systemd_config(tmp_path: Path, unit: str, *, timeout: float = 10.0) -> Path:
    config = tmp_path / "cli.yaml"
    config.write_text(
        f"""schema_version: 1
cli:
  timeout_seconds: {timeout}
  output_format: json
services:
  rt:
    manager: systemd
    target: {unit}
    systemd_scope: user
""",
        encoding="utf-8",
    )
    return config


def test_systemd_user_status_active(user_unit: str, tmp_path: Path) -> None:
    """S01：真实 D-Bus GetUnit + ActiveState 读取。"""
    result = _cli(_systemd_config(tmp_path, user_unit), "service", "status", "rt")
    assert result.returncode == 3, result.stdout
    process = _json(result)["data"][0]["process"]
    assert process["active_state"] == "active"
    assert process["status"] == "ACTIVE"


def test_systemd_user_stop_start_confirmed(user_unit: str, tmp_path: Path) -> None:
    """S01/S02：stop/start 经 Manager.StopUnit/StartUnit 并轮询确认到期望态。"""
    config = _systemd_config(tmp_path, user_unit)
    stopped = _cli(config, "service", "stop", "rt", "--yes")
    assert stopped.returncode == 0, stopped.stdout
    assert (
        subprocess.run(
            ["systemctl", "--user", "is-active", user_unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        == "inactive"
    )
    started = _cli(config, "service", "start", "rt", "--yes")
    assert started.returncode == 3, started.stdout  # 进程成功；无业务端点→UNKNOWN/3
    stages = _json(started)["data"]["process"]["stages"]
    assert [stage["operation"] for stage in stages] == ["start"]
    assert stages[0]["active_state"] == "active"
    assert stages[0]["confirmed"] is True


def test_systemd_user_restart_confirmed(user_unit: str, tmp_path: Path) -> None:
    """S01：restart = StopUnit→inactive→StartUnit→active 全阶段确认。"""
    result = _cli(_systemd_config(tmp_path, user_unit), "service", "restart", "rt", "--yes")
    assert result.returncode in {0, 3}, result.stdout
    stages = _json(result)["data"]["process"]["stages"]
    assert [stage["operation"] for stage in stages] == ["stop", "start"]
    assert [stage["active_state"] for stage in stages] == ["inactive", "active"]


def test_systemd_user_vanished_unit_start_is_failure(tmp_path: Path) -> None:
    """S03：单元在停止后被 GC（瞬态单元）→ start 必须 FAILURE/1，绝不伪称已启动。"""
    if not _SYSTEMD_USER:
        pytest.skip("NOT_RUN: 本机无 systemd --user 总线")
    unit = f"whe2e-gone-{uuid.uuid4().hex[:8]}.service"
    created = subprocess.run(
        ["systemd-run", "--user", "--unit", unit, "--", "true"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    for _ in range(50):
        state_text = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        if state_text in {"inactive", "deactivated"}:
            break
        time.sleep(0.1)
    try:
        result = _cli(_systemd_config(tmp_path, unit), "service", "start", "rt", "--yes")
        assert result.returncode == 1, result.stdout
        assert _json(result)["status"] == "FAILURE"
        assert "not found" in _json(result)["message"]
    finally:
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit], capture_output=True, check=False
        )


def test_service_status_reports_stopped_container_as_degraded(cid: str, tmp_path: Path) -> None:
    """S03：容器进程已停时 service status 必须按 state=STOPPED 计降级，
    不得只凭业务端口应答就掩盖进程状态（对应 runtime 降级键修复）。"""
    _run_task(cid, "sleep", "3000")
    config = _write_config(tmp_path, cid)
    running = _cli(config, "service", "status", "rt")
    assert running.returncode == 3
    assert _json(running)["data"][0]["process"]["state"] == "RUNNING"

    _ctr("task", "kill", "-s", "SIGKILL", cid)
    for _ in range(30):
        if _task_state(cid) == "STOPPED":
            break
        time.sleep(0.2)
    else:
        raise AssertionError("task did not reach STOPPED")

    stopped = _cli(config, "service", "status", "rt")
    assert stopped.returncode == 3
    process = _json(stopped)["data"][0]["process"]
    assert process["state"] == "STOPPED"
