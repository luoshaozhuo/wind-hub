"""pyads 共享连接并发能力的隔离子进程实验 Worker。

用法::

    python -m tests.support.ads_concurrency_child '<spec-json>'

子进程**自托管** ``ControlledAdsMemoryServer`` 并用真实 pyads/AdsLib 经
真实 TCP 执行一个并发场景，结束时向 stdout 打印单行 JSON 证据后退出。
无锁并发存在段错误/挂死风险，必须在独立进程中运行，由父进程
（pytest 或 CLI）施加硬超时与退出码检查。

退出码：
  0 实验正常完成（注意：无锁模式出现 ADSError 也算"实验完成"，
    安全性结论由 JSON 证据判定，而非退出码）；
  2 实验本身失败（参数/装配错误）；
  4 检测到线程挂死，worker 未在截止时间内收敛。

spec 字段：
  scenario: A01..A10
  mode: unlocked | locked | sequential
  ip: 服务绑定/客户端连接的 loopback 地址
  threads: 并发线程数（sequential 模式为单线程调用次数倍率）
  iters: 每线程执行轮数
  delay_ms: 目标命令的服务端应答延迟
  fault: none | timeout | disconnect | reject
  timeout_ms: pyads 客户端命令超时
  seed: 随机种子（当前场景保留位）
"""

from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import struct
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from ctypes import c_ubyte
from dataclasses import asdict, dataclass, field
from typing import Any

import pyads  # type: ignore[import-untyped]
from pyads import constants

from tests.support.ads_controlled_server import (
    ControlledAdsMemoryServer,
    ControlledMemoryHandler,
)
from tests.support.ads_memory_server import pack_points

_GROUP_READ = 0x4020
_GROUP_WRITE = 0x4021
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"
_TARGET_PORT = 851

# 实验整体硬上界（秒）；正常矩阵单 cell 远小于此。
_HARD_DEADLINE = 25.0


@dataclass(slots=True)
class OpResult:
    ok: bool = 0  # type: ignore[assignment]
    err_type: str = ""
    err_code: int | None = None
    latency: float = 0.0
    detail: str = ""


@dataclass(slots=True)
class ExperimentResult:
    scenario: str
    mode: str
    threads: int
    iters: int
    delay_ms: float
    fault: str
    pyads_version: str
    wall_s: float = 0.0
    hung: bool = False
    success: int = 0
    failure: int = 0
    mismatch: int = 0
    error_hist: dict[str, int] = field(default_factory=dict)
    latencies_ms: dict[str, float] = field(default_factory=dict)
    values_seen: list[float] = field(default_factory=list)
    writes_landed: int | None = None
    writes_expected: int | None = None
    notification_added: int = 0
    notification_removed: int = 0
    notification_callbacks: int = 0
    residual_notifications: int = 0
    server_requests: int = 0
    server_command_counts: dict[str, int] = field(default_factory=dict)
    server_invoke_ids: dict[str, list[int]] = field(default_factory=dict)
    server_peak_active: dict[str, int] = field(default_factory=dict)
    wire_frames_per_thread: int = 0
    stderr_tail: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


def _latency_stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        "count": float(len(ordered)),
        "min": round(ordered[0] * 1000, 3),
        "p50": round(ordered[len(ordered) // 2] * 1000, 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000, 3),
        "max": round(ordered[-1] * 1000, 3),
    }


class _Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.results: list[OpResult] = []
        self.values: list[float] = []
        self.added = 0
        self.removed = 0
        self.callbacks = 0

    def record(self, result: OpResult) -> None:
        with self.lock:
            self.results.append(result)

    def value(self, value: float) -> None:
        with self.lock:
            self.values.append(value)


def _sum_read(conn: Any, items: list[tuple[int, int, int]]) -> bytes:
    raw_items = b"".join(struct.pack("<III", *item) for item in items)
    buf = (c_ubyte * len(raw_items))(*raw_items)
    raw = conn.read_write(
        constants.ADSIGRP_SUMUP_READ,
        len(items),
        None,
        buf,
        None,
        check_length=False,
    )
    return bytes(raw)


def _sum_write(conn: Any, items: list[tuple[int, int, bytes]]) -> int:
    headers = b"".join(
        struct.pack("<III", group, offset, len(payload)) for group, offset, payload in items
    )
    payload = b"".join(payload for _, _, payload in items)
    raw = conn.read_write(
        constants.ADSIGRP_SUMUP_WRITE,
        len(items),
        None,
        bytearray(headers + payload),
        None,
        check_length=False,
    )
    codes = struct.unpack(f"<{len(items)}I", bytes(raw))
    return sum(1 for code in codes if code == 0)


def _drain_stderr() -> tuple[int, int, Any]:
    """把 C 层 fprintf 的 stderr 重定向到管道并后台排空，返回 (oldfd, readfd)。"""
    import fcntl

    read_fd, write_fd = os.pipe()
    old_fd = os.dup(2)
    os.dup2(write_fd, 2)
    os.close(write_fd)
    flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
    fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    captured: list[bytes] = []

    def pump() -> None:
        while True:
            try:
                chunk = os.read(read_fd, 4096)
            except OSError:
                time.sleep(0.01)
                continue
            if not chunk:
                return
            captured.append(chunk)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return old_fd, read_fd, captured


def _restore_stderr(old_fd: int, read_fd: int, captured: list[bytes]) -> str:
    os.dup2(old_fd, 2)
    os.close(old_fd)
    time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.close(read_fd)
    text = b"".join(captured)[-4000:].decode("utf-8", "replace")
    return text


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    spec: dict[str, Any]
    server: ControlledAdsMemoryServer
    conns: list[Any]
    counters: _Counters
    gate: threading.Barrier | None
    io_lock: threading.Lock
    notification_handles: list[Any] = field(default_factory=list)
    handles_lock: threading.Lock = field(default_factory=threading.Lock)

    def gate_wait(self) -> None:
        if self.gate is not None:
            self.gate.wait(timeout=_HARD_DEADLINE)

    @contextlib.contextmanager
    def serialize(self) -> Any:
        if self.spec["mode"] == "locked":
            with self.io_lock:
                yield
        else:
            yield


def _seed(server: ControlledAdsMemoryServer, n: int) -> None:
    points = max(n, 64)
    server.set_values(
        _GROUP_READ,
        0,
        pack_points([("REAL", float(i)) for i in range(points)]),
    )
    server.set_values(
        _GROUP_WRITE,
        0,
        b"\x00" * (points * 4),
    )


def _make_callback(ctx: _Ctx) -> Callable[..., None]:
    def _callback(*_args: Any) -> None:
        with ctx.counters.lock:
            ctx.counters.callbacks += 1

    return _callback


def _op_read(ctx: _Ctx, index: int) -> None:
    conn = ctx.conns[0]
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            value = conn.read(_GROUP_READ, (index % 64) * 4, pyads.PLCTYPE_REAL)
        result = OpResult(ok=True, latency=time.monotonic() - t0)
        ctx.counters.value(float(value))
        if float(value) != float(index % 64):
            result.detail = f"expected {float(index % 64)} got {float(value)}"
    except Exception as exc:  # noqa: BLE001 - 实验记录所有异常
        result = OpResult(
            err_type=type(exc).__name__,
            err_code=getattr(exc, "err_code", None),
            latency=time.monotonic() - t0,
            detail=str(exc)[:120],
        )
    ctx.counters.record(result)


def _op_read_state(ctx: _Ctx, index: int) -> None:
    del index
    conn = ctx.conns[0]
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            state = conn.read_state()
        ok = state is not None and len(state) == 2
        result = OpResult(ok=ok, latency=time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001
        result = OpResult(
            err_type=type(exc).__name__,
            err_code=getattr(exc, "err_code", None),
            latency=time.monotonic() - t0,
            detail=str(exc)[:120],
        )
    ctx.counters.record(result)


def _op_write(ctx: _Ctx, index: int) -> None:
    conn = ctx.conns[0]
    offset = (index % 64) * 4
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            conn.write(_GROUP_WRITE, offset, float(index) + 0.25, pyads.PLCTYPE_REAL)
        result = OpResult(ok=True, latency=time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001
        result = OpResult(
            err_type=type(exc).__name__,
            err_code=getattr(exc, "err_code", None),
            latency=time.monotonic() - t0,
            detail=str(exc)[:120],
        )
    ctx.counters.record(result)


def _op_read_write(ctx: _Ctx, index: int) -> None:
    """read_write 命令：单项 Sum Read（Driver 对 read_write 的真实用法）。"""
    conn = ctx.conns[0]
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            raw = _sum_read(conn, [(_GROUP_READ, (index % 64) * 4, 4)])
        value = struct.unpack("<f", raw[4:8])[0]
        result = OpResult(ok=True, latency=time.monotonic() - t0)
        ctx.counters.value(value)
        if value != float(index % 64):
            result.detail = f"expected {float(index % 64)} got {value}"
    except Exception as exc:  # noqa: BLE001
        result = OpResult(
            err_type=type(exc).__name__,
            err_code=getattr(exc, "err_code", None),
            latency=time.monotonic() - t0,
            detail=str(exc)[:120],
        )
    ctx.counters.record(result)


def _op_sum_read(ctx: _Ctx, index: int) -> None:
    conn = ctx.conns[0]
    items = [(_GROUP_READ, ((index * 4 + j) % 60) * 4, 4) for j in range(4)]
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            raw = _sum_read(conn, items)
        values = [struct.unpack("<f", raw[16 + k * 4 : 20 + k * 4])[0] for k in range(4)]
        ctx.counters.record(OpResult(ok=True, latency=time.monotonic() - t0))
        for value in values:
            ctx.counters.value(value)
    except Exception as exc:  # noqa: BLE001
        ctx.counters.record(
            OpResult(
                err_type=type(exc).__name__,
                err_code=getattr(exc, "err_code", None),
                latency=time.monotonic() - t0,
                detail=str(exc)[:120],
            )
        )


def _op_sum_write(ctx: _Ctx, index: int) -> None:
    conn = ctx.conns[0]
    items = [
        (_GROUP_WRITE, ((index * 2) % 60) * 4, struct.pack("<f", float(index) + 0.5)),
        (_GROUP_WRITE, ((index * 2 + 1) % 60) * 4, struct.pack("<f", float(index) + 1.5)),
    ]
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            landed = _sum_write(conn, items)
        ctx.counters.record(
            OpResult(
                ok=landed == 2,
                latency=time.monotonic() - t0,
                detail="" if landed == 2 else f"landed {landed}/2",
            )
        )
    except Exception as exc:  # noqa: BLE001
        ctx.counters.record(
            OpResult(
                err_type=type(exc).__name__,
                err_code=getattr(exc, "err_code", None),
                latency=time.monotonic() - t0,
                detail=str(exc)[:120],
            )
        )


def _register_notification(ctx: _Ctx, index: int) -> None:
    conn = ctx.conns[0]
    attr = pyads.NotificationAttrib(
        4,
        trans_mode=pyads.ADSTRANS_SERVERCYCLE,
        cycle_time=10,
        max_delay=10,
    )
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            handle = conn.add_device_notification(
                (_GROUP_READ, (index % 50) * 8), attr, _make_callback(ctx)
            )
        with ctx.handles_lock:
            ctx.notification_handles.append(handle)
            ctx.counters.added += 1
        ctx.counters.record(OpResult(ok=True, latency=time.monotonic() - t0))
    except Exception as exc:  # noqa: BLE001
        ctx.counters.record(
            OpResult(
                err_type=type(exc).__name__,
                err_code=getattr(exc, "err_code", None),
                latency=time.monotonic() - t0,
                detail=str(exc)[:120],
            )
        )


def _deregister_notification(ctx: _Ctx, index: int) -> None:
    del index
    with ctx.handles_lock:
        handle = ctx.notification_handles.pop() if ctx.notification_handles else None
    if handle is None:
        return
    conn = ctx.conns[0]
    t0 = time.monotonic()
    try:
        with ctx.serialize():
            conn.del_device_notification(*handle)
        ctx.counters.removed += 1
        ctx.counters.record(OpResult(ok=True, latency=time.monotonic() - t0))
    except Exception as exc:  # noqa: BLE001
        ctx.counters.record(
            OpResult(
                err_type=type(exc).__name__,
                err_code=getattr(exc, "err_code", None),
                latency=time.monotonic() - t0,
                detail=str(exc)[:120],
            )
        )


# 每个场景：线程角色函数序列（按线程索引循环分配）与需要慢化的命令。
def _scenario_workers(ctx: _Ctx) -> tuple[list[Callable[[_Ctx, int], None]], int]:
    """返回 (worker 角色表, 被慢化/故障注入的服务端命令 id)。"""
    cmd_read = ControlledMemoryHandler.command_id("READ")
    cmd_rw = ControlledMemoryHandler.command_id("READWRITE")
    cmd_state = ControlledMemoryHandler.command_id("READSTATE")
    cmd_add = ControlledMemoryHandler.command_id("ADDDEVICENOTE")
    cmd_del = ControlledMemoryHandler.command_id("DELDEVICENOTE")

    scenario = ctx.spec["scenario"]
    if scenario == "A01":
        return [_op_read_state, _op_read], cmd_state
    if scenario == "A02":
        return [_op_read, _op_write], cmd_read
    if scenario == "A03":
        return [_op_read], cmd_read
    if scenario == "A04":
        return [_op_read_write], cmd_rw
    if scenario == "A05":
        return [_op_sum_read, _op_sum_write], cmd_rw
    if scenario == "A06":
        return [_register_notification, _op_read], cmd_add
    if scenario == "A07":
        return [_deregister_notification, _op_read_state], cmd_del
    if scenario == "A08":
        return [_op_read, _op_read_state], cmd_read
    if scenario == "A09":
        return [_op_read, _op_write, _op_read_state, _op_read_write], cmd_read
    if scenario == "A10":
        # 实际线程主体在 run_experiment 中按独立连接分派；慢化命令仍为 READ。
        return [_op_read], cmd_read
    raise ValueError(f"unknown scenario {scenario}")


def _prepare_scenario(ctx: _Ctx, slow_command: int) -> None:
    spec = ctx.spec
    handler = ctx.server.control
    delay_s = spec["delay_ms"] / 1000.0
    if spec["fault"] == "reject":
        handler.set_reject(slow_command, 1808)
    elif spec["fault"] == "timeout":
        handler.set_delay(slow_command, 5.0)
    elif delay_s:
        handler.set_delay(slow_command, delay_s)

    if ctx.spec["scenario"] == "A07":
        # 预注册一批通知，注销线程在实验中删除。
        attr = pyads.NotificationAttrib(
            4, trans_mode=pyads.ADSTRANS_SERVERCYCLE, cycle_time=10_000, max_delay=10_000
        )
        count = spec["threads"] * spec["iters"]
        for index in range(count):
            handle = ctx.conns[0].add_device_notification(
                (_GROUP_READ, 400 + index * 8), attr, _make_callback(ctx)
            )
            ctx.notification_handles.append(handle)
        ctx.counters.added += count
    if ctx.spec["scenario"] == "A08":
        # 高频周期通知在整个实验期间持续投递，回调线程与同步命令并发。
        attr = pyads.NotificationAttrib(
            4, trans_mode=pyads.ADSTRANS_SERVERCYCLE, cycle_time=10, max_delay=10
        )
        for index in range(min(4, spec["threads"])):
            handle = ctx.conns[0].add_device_notification(
                (_GROUP_READ, 900 + index * 8), attr, _make_callback(ctx)
            )
            ctx.notification_handles.append(handle)
        ctx.counters.added += min(4, spec["threads"])


def run_experiment(spec: dict[str, Any]) -> ExperimentResult:
    scenario = spec["scenario"]
    mode = spec["mode"]
    n_threads = int(spec["threads"])
    iters = int(spec["iters"])

    result = ExperimentResult(
        scenario=scenario,
        mode=mode,
        threads=n_threads,
        iters=iters,
        delay_ms=float(spec["delay_ms"]),
        fault=str(spec.get("fault", "none")),
        pyads_version=str(pyads.__version__),
    )

    server = ControlledAdsMemoryServer(ip_address=spec["ip"])
    old_fd = read_fd = None
    captured: list[bytes] = []
    try:
        server.start()
        _seed(server, 64)
        pyads.open_port()
        pyads.set_local_address(_LOCAL_NET_ID)

        connections: list[Any] = []
        if scenario == "A10":
            # 多独立连接：每个线程一条自己的 pyads Connection。
            for _ in range(n_threads):
                conn = pyads.Connection(_NET_ID, _TARGET_PORT, spec["ip"])
                conn.open()
                conn.set_timeout(int(spec["timeout_ms"]))
                connections.append(conn)
        else:
            conn = pyads.Connection(_NET_ID, _TARGET_PORT, spec["ip"])
            conn.open()
            conn.set_timeout(int(spec["timeout_ms"]))
            connections.append(conn)

        ctx = _Ctx(
            spec=spec,
            server=server,
            conns=connections,
            counters=_Counters(),
            gate=None,
            io_lock=threading.Lock(),
        )
        roles, slow_command = _scenario_workers(ctx)
        _prepare_scenario(ctx, slow_command)

        old_fd, read_fd, captured = _drain_stderr()
        t_start = time.monotonic()

        # 断线故障：屏障放行后 50ms 强拆服务端 TCP（模拟中途断链）。
        if spec.get("fault") == "disconnect":

            def _drop() -> None:
                time.sleep(0.05)
                server._stop_tcp()  # noqa: SLF001 - 测试工具内部协作

            threading.Thread(target=_drop, name="ads-exp-fault", daemon=True).start()

        threads: list[threading.Thread] = []
        if scenario == "A10":
            # 每线程独占一条独立 pyads Connection。
            ctx.gate = threading.Barrier(n_threads)

            def thread_main(index: int) -> None:
                ctx.gate_wait()
                for iteration in range(iters):
                    _op_read_on(ctx, index, connections[index], iteration)

            threads = [
                threading.Thread(target=thread_main, args=(i,), name=f"ads-exp-{i}")
                for i in range(n_threads)
            ]
        elif mode == "sequential":
            for call_index in range(n_threads * iters):
                role = roles[call_index % len(roles)]
                role(ctx, call_index)
        else:
            ctx.gate = threading.Barrier(n_threads)

            def thread_main(index: int) -> None:
                role = roles[index % len(roles)]
                ctx.gate_wait()
                for iteration in range(iters):
                    role(ctx, index + iteration * n_threads)

            threads = [
                threading.Thread(target=thread_main, args=(i,), name=f"ads-exp-{i}")
                for i in range(n_threads)
            ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_HARD_DEADLINE)
        hung = [thread.name for thread in threads if thread.is_alive()]
        if hung:
            result.hung = True
            result.notes.append(f"hung threads: {hung}")
            faulthandler.dump_traceback()
            result.wall_s = round(time.monotonic() - t_start, 3)
            result.stderr_tail = _restore_stderr(old_fd, read_fd, captured)
            _finalize(server, connections, ctx, result)
            return result

        result.wall_s = round(time.monotonic() - t_start, 3)
        result.stderr_tail = _restore_stderr(old_fd, read_fd, captured)
        old_fd = read_fd = None

        _post_scenario(ctx, result)
        _finalize(server, connections, ctx, result)
        return result
    finally:
        if old_fd is not None and read_fd is not None:
            with contextlib.suppress(Exception):
                result.stderr_tail = _restore_stderr(old_fd, read_fd, captured)
        with contextlib.suppress(Exception):
            pyads.close_port()
        with contextlib.suppress(Exception):
            server.stop()


def _op_read_on(ctx: _Ctx, index: int, conn: Any, iteration: int) -> None:
    t0 = time.monotonic()
    slot = (index + iteration) % 64
    try:
        value = conn.read(_GROUP_READ, slot * 4, pyads.PLCTYPE_REAL)
        result = OpResult(ok=True, latency=time.monotonic() - t0)
        if float(value) != float(slot):
            result.detail = f"expected {float(slot)} got {float(value)}"
        ctx.counters.value(float(value))
    except Exception as exc:  # noqa: BLE001
        result = OpResult(
            err_type=type(exc).__name__,
            err_code=getattr(exc, "err_code", None),
            latency=time.monotonic() - t0,
            detail=str(exc)[:120],
        )
    ctx.counters.record(result)


def _post_scenario(ctx: _Ctx, result: ExperimentResult) -> None:
    counters = ctx.counters
    latencies = [r.latency for r in counters.results if r.ok]
    result.latencies_ms = _latency_stats(latencies)
    result.success = sum(1 for r in counters.results if r.ok)
    result.failure = sum(1 for r in counters.results if not r.ok)
    result.mismatch = sum(1 for r in counters.results if r.detail.startswith("expected"))
    hist: Counter[str] = Counter()
    for r in counters.results:
        if not r.ok:
            hist[f"{r.err_type}:{r.err_code}"] += 1
    result.error_hist = dict(hist)
    result.values_seen = counters.values[:200]
    result.notification_added = counters.added
    result.notification_removed = counters.removed
    result.notification_callbacks = counters.callbacks

    # 写值终态校验（服务端直接读内存，独立于客户端通道）。
    if ctx.spec["scenario"] in {"A02", "A05"}:
        server = ctx.server
        landed = 0
        checked = 0
        for index in range(64):
            data = server.control.read_memory(_GROUP_WRITE, index * 4, 4)
            if data is None:
                continue
            checked += 1
            value = struct.unpack("<f", data)[0]
            if value != 0.0:
                landed += 1
        result.writes_landed = landed
        result.writes_expected = checked


def _finalize(
    server: ControlledAdsMemoryServer,
    connections: list[Any],
    ctx: _Ctx,
    result: ExperimentResult,
) -> None:
    # 尝试清理通知（实验本身可能已使通道失效；失败只记录）。
    for handle in list(ctx.notification_handles):
        with contextlib.suppress(Exception):
            connections[0].del_device_notification(*handle)
    for conn in connections:
        with contextlib.suppress(Exception):
            conn.close()
    time.sleep(0.05)
    result.residual_notifications = server.notification_count()
    result.server_requests = server.handler.request_count
    from tests.support.ads_controlled_server import command_name

    counts: Counter[str] = Counter()
    peaks: dict[str, int] = {}
    invokes: dict[str, list[int]] = {}
    for record in server.control.records():
        name = command_name(record.command_id)
        counts[name] += 1
        invokes.setdefault(name, []).append(record.invoke_id)
        peaks[name] = max(peaks.get(name, 0), record.active_at_dispatch)
    result.server_command_counts = dict(counts)
    result.server_peak_active = peaks
    result.server_invoke_ids = invokes


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tests.support.ads_concurrency_child <spec-json>", file=sys.stderr)
        return 2
    try:
        spec = json.loads(argv[1])
        required = {"scenario", "mode", "ip", "threads", "iters", "delay_ms"}
        missing = required - set(spec)
        if missing:
            raise ValueError(f"missing spec keys: {sorted(missing)}")
        spec.setdefault("fault", "none")
        spec.setdefault("timeout_ms", 2000)
        spec.setdefault("seed", 0)
        if spec["scenario"] not in {f"A{i:02d}" for i in range(1, 11)}:
            raise ValueError(f"bad scenario {spec['scenario']}")
        if spec["mode"] not in {"unlocked", "locked", "sequential"}:
            raise ValueError(f"bad mode {spec['mode']}")
        result = run_experiment(spec)
        print(result.to_json())
        return 4 if result.hung else 0
    except Exception as exc:  # noqa: BLE001
        print(f"experiment harness error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
