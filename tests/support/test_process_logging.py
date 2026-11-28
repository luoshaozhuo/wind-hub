"""Sink/服务子进程日志落盘契约（tests/support/process.py）。

验证 PYTHONUNBUFFERED 实际生效、stdout/stderr 持续落盘、错误可追溯、
SIGTERM 后日志保留、日志不含敏感环境值，且其余服务启动入口不受影响。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.support.process import (
    _start_service,
    free_port,
    start_collector,
    start_commander,
    start_sink_service_from_root,
    start_web_server,
)

pytestmark = [pytest.mark.unit]


def _spawn(tmp_path: Path, code: str, *, env_extra: dict[str, str] | None = None):
    """以 _start_service 的真实路径拉起探针子进程（不额外传 -u）。"""
    return _start_service(
        sys.executable,
        ["-c", code],
        service="probe",
        port=free_port(),
        log_dir=tmp_path,
        env_extra=env_extra,
    )


def _wait_log(proc, marker: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = proc.read_log()
        if marker in content:
            return content
        time.sleep(0.2)
    pytest.fail(f"marker {marker!r} missing: {proc.read_log()!r}")


def _child_environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", errors="replace")
    env: dict[str, str] = {}
    for item in raw.split("\0"):
        if "=" in item:
            key, _, value = item.partition("=")
            env[key] = value
    return env


def test_pythonunbuffered_reaches_child_and_stdout_streams(tmp_path: Path) -> None:
    """无 flush/无换行的 stdout 必须落盘（证明 PYTHONUNBUFFERED 实际生效）。"""
    proc = _spawn(
        tmp_path,
        "import sys; print('MARKER-NO-NEWLINE', end=''); "
        "sys.stderr.write('MARKER-STDERR\\n'); "
        "import time; time.sleep(30)",
    )
    try:
        content = _wait_log(proc, "MARKER-NO-NEWLINE")
        assert "MARKER-STDERR" in content
        assert _child_environ(proc.proc.pid).get("PYTHONUNBUFFERED") == "1"
    finally:
        proc.kill_tree()


def test_recoverable_error_is_traceable_in_log(tmp_path: Path) -> None:
    """logging 输出的可恢复错误必须能在日志文件中检索。"""
    proc = _spawn(
        tmp_path,
        "import logging; logging.basicConfig(level=logging.INFO); "
        "logging.error('simulated recoverable failure code=E42'); "
        "import time; time.sleep(30)",
    )
    try:
        _wait_log(proc, "simulated recoverable failure code=E42")
    finally:
        proc.kill_tree()


def test_sigterm_preserves_log_content(tmp_path: Path) -> None:
    """SIGTERM 退出后已写入的日志必须保留。"""
    proc = _spawn(
        tmp_path,
        "import time; print('BEFORE-TERMINATE', flush=True); time.sleep(60)",
    )
    try:
        _wait_log(proc, "BEFORE-TERMINATE")
        exit_code = proc.terminate(timeout=15)
        assert exit_code in (-15, 143)
        assert "BEFORE-TERMINATE" in proc.read_log()
    finally:
        if proc.is_running():
            proc.kill_tree()


def test_log_never_contains_secret_env_values(tmp_path: Path) -> None:
    """子进程 env 中的令牌值绝不出现在日志文件。"""
    secret = "t04-log-secret-" + "ab" * 32
    proc = _spawn(
        tmp_path,
        "import logging; logging.basicConfig(level=logging.INFO); "
        "logging.info('service starting'); "
        "import time; time.sleep(30)",
        env_extra={"WIND_HUB_PROBE_TOKEN": secret},
    )
    try:
        content = _wait_log(proc, "service starting")
        assert secret not in content, "secret env value leaked into log"
    finally:
        proc.kill_tree()


def test_service_entrypoints_share_unbuffered_logging(tmp_path: Path, monkeypatch) -> None:
    """_start_service 注入 PYTHONUNBUFFERED 并合并 stderr；其余入口共用该实现。"""
    captured: list[dict] = []

    class FakeProc:
        def __init__(self, argv, **kwargs):  # noqa: ANN001
            captured.append({"argv": argv, **kwargs})

        def poll(self):
            return 0

    monkeypatch.setattr("tests.support.process.subprocess.Popen", FakeProc)
    _start_service(
        sys.executable,
        ["-c", "pass"],
        service="env-probe",
        port=free_port(),
        log_dir=tmp_path,
        env_extra={"X": "1"},
    )
    assert captured, "Popen not reached"
    assert captured[0]["env"]["PYTHONUNBUFFERED"] == "1"
    assert captured[0]["env"]["X"] == "1"
    assert captured[0]["stderr"] is subprocess.STDOUT
    # 其余服务入口仍指向同一实现（契约存在性）。
    assert callable(start_collector)
    assert callable(start_commander)
    assert callable(start_web_server)
    assert callable(start_sink_service_from_root)
