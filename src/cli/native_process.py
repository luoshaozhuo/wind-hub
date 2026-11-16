"""Manage explicitly configured Linux child services without an external supervisor."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .runtime import ServiceTarget


def _identity(pid: int) -> str | None:
    """Read the Linux PID's creation tick; reject zombies and absent processes."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    tail = stat.rsplit(") ", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) < 20 or fields[0] == "Z":
        return None
    return fields[19]


def _group_active(pgid: int) -> bool:
    """Check all non-zombie members, including workers after leader exit."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            fields = stat.rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 2 and fields[0] != "Z" and fields[2] == str(pgid):
            return True
    return False


def _private_owned(metadata: os.stat_result) -> bool:
    return metadata.st_uid == os.geteuid() and not metadata.st_mode & 0o077


def _terminate_untracked_group(pgid: int, deadline: float) -> None:
    """Ensure a just-started, unrecorded process group cannot escape management."""
    os.killpg(pgid, signal.SIGTERM)
    while _group_active(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_active(pgid):
        os.killpg(pgid, signal.SIGKILL)
        while _group_active(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
    if _group_active(pgid):
        raise RuntimeError("could not clean up untracked process group")


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"unreadable process state: {path}") from exc
    if not isinstance(state, dict) or type(state.get("pid")) is not int:
        raise RuntimeError(f"invalid process state: {path}")
    if not isinstance(state.get("start_tick"), str):
        raise RuntimeError(f"invalid process identity: {path}")
    return state


def _running(state: dict[str, Any] | None) -> bool:
    if state is None:
        return False
    pid = state["pid"]
    start_tick = state["start_tick"]
    if not isinstance(pid, int) or not isinstance(start_tick, str):
        return False
    if _identity(pid) != start_tick:
        return False
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _manage(target: ServiceTarget, action: str) -> dict[str, Any]:
    spec = target.process
    if spec is None:
        raise ValueError("process manager requires a validated process specification")
    state_dir = spec.state_dir
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = state_dir.stat()
    if not _private_owned(metadata):
        raise RuntimeError("process state directory must be private to the CLI user")
    state_file = state_dir / f"{target.service_id}.json"
    lock_file = state_dir / f"{target.service_id}.lock"
    deadline = time.monotonic() + target.timeout

    if (
        not target.service_id.isascii()
        or not target.service_id.replace("-", "").replace("_", "").isalnum()
    ):
        raise ValueError("invalid process service identity")

    def current_state() -> dict[str, Any]:
        saved = _load_state(state_file)
        alive = _running(saved)
        return {
            "manager": "process",
            "target": target.service_id,
            "pid": saved["pid"] if alive and saved is not None else None,
            "running": alive,
            "state": "RUNNING" if alive else "STOPPED",
            "status": "ACTIVE" if alive else "INACTIVE",
        }

    def start() -> dict[str, Any]:
        existing = current_state()
        if existing["running"]:
            return {**existing, "confirmed": True, "already_running": True}
        spec.log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_fd = os.open(
            spec.log_file,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            log_metadata = os.fstat(log_fd)
            if not _private_owned(log_metadata) or not stat.S_ISREG(log_metadata.st_mode):
                raise RuntimeError("process log file must be a private regular file")
        except BaseException:
            os.close(log_fd)
            raise
        try:
            with os.fdopen(log_fd, "ab", buffering=0) as logfile:
                child = subprocess.Popen(
                    list(spec.argv),
                    cwd=spec.cwd,
                    env={**os.environ, **spec.env},
                    stdin=subprocess.DEVNULL,
                    stdout=logfile,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
        except OSError as exc:
            raise RuntimeError("could not start configured process") from exc
        tick = _identity(child.pid)
        if tick is None:
            raise RuntimeError("started process exited before identity confirmation")
        try:
            _save_state(state_file, {"pid": child.pid, "start_tick": tick})
        except OSError:
            _terminate_untracked_group(child.pid, deadline)
            raise
        state = current_state()
        if not state["running"]:
            raise RuntimeError("process exited during start confirmation")
        return {
            **state,
            "confirmed": True,
            "stages": [{"operation": "start", "confirmed": True}],
        }

    def stop() -> dict[str, Any]:
        saved = _load_state(state_file)
        if not _running(saved):
            if saved is not None and _group_active(saved["pid"]):
                raise RuntimeError(
                    "orphaned or conflicting worker group requires explicit recovery"
                )
            state_file.unlink(missing_ok=True)
            return {**current_state(), "confirmed": True, "already_stopped": True}
        assert saved is not None
        pid = saved["pid"]
        # No signal is sent until both PID start tick and its own process group agree.
        if os.getpgid(pid) != pid:
            raise RuntimeError("process group identity changed")
        os.killpg(pid, signal.SIGTERM)
        while _group_active(pid):
            if time.monotonic() >= deadline:
                raise TimeoutError("process group stop not confirmed")
            time.sleep(0.1)
        state_file.unlink(missing_ok=True)
        return {
            **current_state(),
            "confirmed": True,
            "stages": [{"operation": "stop", "confirmed": True}],
        }

    with _locked(lock_file):
        if action == "status":
            return current_state()
        if action == "start":
            return start()
        if action == "stop":
            return stop()
        if action == "restart":
            stopped = stop()
            started = start()
            return {
                **started,
                "stages": [*stopped.get("stages", []), *started.get("stages", [])],
            }
        raise ValueError(f"unsupported process action: {action}")


async def manage_native_process(target: ServiceTarget, action: str) -> dict[str, Any]:
    """Run potentially blocking process operations away from the CLI event loop."""
    return await asyncio.to_thread(_manage, target, action)
