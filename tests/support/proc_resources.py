"""基于 Linux /proc 的进程树身份核验与无阈值资源采样。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """PID 必须配合 start tick，避免把 PID 复用误报为原进程未释放。"""

    pid: int
    start_ticks: int
    state: str
    command: str


def process_identity(pid: int) -> ProcessIdentity | None:
    root = Path("/proc") / str(pid)
    try:
        stat = (root / "stat").read_text(encoding="utf-8")
        command = (root / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm 可包含空格和括号；右括号后的 state 是第 3 列，starttime 是第 22 列。
    after = stat[stat.rindex(")") + 2 :].split()
    return ProcessIdentity(
        pid=pid,
        start_ticks=int(after[19]),
        state=after[0],
        command=command.strip(),
    )


def capture_process_tree(root_pid: int) -> dict[int, ProcessIdentity]:
    """捕获调用时真实存在的完整子孙树，包括 multiprocessing resource_tracker。"""
    identities: dict[int, ProcessIdentity] = {}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        identity = process_identity(pid)
        if identity is None or pid in identities:
            continue
        identities[pid] = identity
        children = Path("/proc") / str(pid) / "task" / str(pid) / "children"
        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            pending.extend(int(item) for item in children.read_text(encoding="utf-8").split())
    if root_pid not in identities:
        raise AssertionError(f"root process {root_pid} does not exist")
    return identities


def capture_process_trees(roots: dict[str, int]) -> dict[str, dict[int, ProcessIdentity]]:
    return {name: capture_process_tree(pid) for name, pid in roots.items()}


def _usage(identity: ProcessIdentity) -> tuple[int, int, int]:
    root = Path("/proc") / str(identity.pid)
    stat = (root / "stat").read_text(encoding="utf-8")
    after = stat[stat.rindex(")") + 2 :].split()
    if int(after[19]) != identity.start_ticks:
        raise AssertionError(f"PID reused during sampling: {identity.pid}")
    cpu_ticks = int(after[11]) + int(after[12])
    statm = (root / "statm").read_text(encoding="utf-8").split()
    rss_bytes = int(statm[1]) * os.sysconf("SC_PAGE_SIZE")
    fd_count = len(list((root / "fd").iterdir()))
    return cpu_ticks, rss_bytes, fd_count


async def sample_process_trees(
    trees: dict[str, dict[int, ProcessIdentity]],
    *,
    duration_seconds: float = 12.0,
    interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """采样 CPU/RSS/FD 和进程数，仅报告观测值，不施加性能阈值。"""
    if not 10.0 <= duration_seconds <= 15.0:
        raise ValueError("resource observation window must be 10-15 seconds")
    if interval_seconds <= 0:
        raise ValueError("sample interval must be positive")

    clock_ticks = os.sysconf("SC_CLK_TCK")
    first: dict[int, tuple[int, int, int]] = {}
    latest: dict[int, tuple[int, int, int]] = {}
    maximum_rss = {pid: 0 for tree in trees.values() for pid in tree}
    maximum_fds = dict(maximum_rss)
    started = time.monotonic()
    previous_services: dict[str, tuple[int, float]] = {}
    timeline: list[dict[str, Any]] = []
    samples = 0
    while True:
        frame_at = time.monotonic()
        frame_services: dict[str, Any] = {}
        for name, tree in trees.items():
            for pid, identity in tree.items():
                try:
                    current = _usage(identity)
                except FileNotFoundError as exc:
                    raise AssertionError(f"process exited during sampling: {pid}") from exc
                previous = latest.get(pid)
                if previous is not None and current[0] < previous[0]:
                    raise AssertionError(f"CPU counters moved backwards: {pid}")
                first.setdefault(pid, current)
                latest[pid] = current
                maximum_rss[pid] = max(maximum_rss[pid], current[1])
                maximum_fds[pid] = max(maximum_fds[pid], current[2])
            cpu_ticks = sum(latest[pid][0] for pid in tree)
            previous_service = previous_services.get(name)
            delta_cpu_percent = (
                None
                if previous_service is None
                else (cpu_ticks - previous_service[0])
                * 100.0
                / clock_ticks
                / (frame_at - previous_service[1])
            )
            frame_services[name] = {
                "cumulative_cpu_seconds": cpu_ticks / clock_ticks,
                "delta_cpu_percent": delta_cpu_percent,
                "rss_bytes": sum(latest[pid][1] for pid in tree),
                "fd_count": sum(latest[pid][2] for pid in tree),
                "process_count": len(tree),
            }
            previous_services[name] = (cpu_ticks, frame_at)
        timeline.append(
            {
                "monotonic_offset_seconds": frame_at - started,
                "services": frame_services,
            }
        )
        samples += 1
        elapsed = time.monotonic() - started
        if elapsed >= duration_seconds:
            break
        await asyncio.sleep(min(interval_seconds, duration_seconds - elapsed))

    elapsed = time.monotonic() - started
    services: dict[str, Any] = {}
    for name, tree in trees.items():
        processes = []
        for pid, identity in sorted(tree.items()):
            delta_ticks = latest[pid][0] - first[pid][0]
            processes.append(
                {
                    "pid": pid,
                    "start_ticks": identity.start_ticks,
                    "command": identity.command,
                    "cpu_percent": delta_ticks * 100.0 / clock_ticks / elapsed,
                    "max_rss_bytes": maximum_rss[pid],
                    "max_fd_count": maximum_fds[pid],
                }
            )
        services[name] = {
            "process_count": len(tree),
            "cpu_percent": sum(item["cpu_percent"] for item in processes),
            "max_rss_bytes": sum(item["max_rss_bytes"] for item in processes),
            "max_fd_count": sum(item["max_fd_count"] for item in processes),
            "processes": processes,
        }
    return {
        "duration_seconds": elapsed,
        "sample_count": samples,
        "interval_seconds": interval_seconds,
        "services": services,
        "timeline": timeline,
    }


async def wait_process_trees_reaped(
    trees: dict[str, dict[int, ProcessIdentity]], *, timeout_seconds: float = 20.0
) -> None:
    """同身份 zombie 也不算释放；PID 消失或 start tick 改变才算原进程回收。"""
    identities = {identity.pid: identity for tree in trees.values() for identity in tree.values()}
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining: dict[int, str] = {}
        for pid, original in identities.items():
            current = process_identity(pid)
            if current is not None and current.start_ticks == original.start_ticks:
                remaining[pid] = current.state
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"process identities not reaped: {remaining}")
        await asyncio.sleep(0.1)
