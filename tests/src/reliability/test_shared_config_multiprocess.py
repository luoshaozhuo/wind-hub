"""S2-2/3/4/5：共享配置协作锁与 undo log 的独立进程验证。

设计 Configuration v1_13 明确要求并发与崩溃验证必须使用真实文件系统和
独立进程，不能以 Mock 代替。本模块全部通过 fork 出的真实子进程演练：

- 写方排他锁（LOCK_EX）与读方共享锁（LOCK_SH）跨进程互斥，读方在真实
  SharedDocumentLoader 路径上阻塞直至写方释放；
- 多文件提交期间，独立读进程只能观察到整代一致的文件集，永不观察中间态；
- 写进程在 marker 存活窗口被 SIGKILL 后，独立读进程 fail-closed；
- 独立进程执行恢复，完整文件集回滚（含新增文件删除），且恢复幂等。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import stat
import threading
import time
import uuid
from pathlib import Path
from queue import Empty

import pytest

from commander.infrastructure.config_commit import (
    _MARKER,
    _STORE,
    commit_config_files,
    recover_config_commit,
)
from configuration.shared_documents import SharedConfigPaths, SharedDocumentLoader
from core.application.errors import ConfigError

pytestmark = [
    pytest.mark.reliability,
    pytest.mark.concurrency,
    pytest.mark.real_service,
    pytest.mark.skipif(
        os.name != "posix",
        reason="fcntl.flock 与 SIGKILL 崩溃窗口验证仅在 POSIX 环境成立",
    ),
]

_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"
_DEVICES_REL = "instances/devices.yaml"
_UNITS_REL = "templates/units.yaml"


def _copy_tree(base: Path) -> Path:
    root = base / "configs"
    import shutil

    shutil.copytree(_REPO_CONFIGS, root)
    return root


def _loader(root: Path) -> SharedDocumentLoader:
    return SharedDocumentLoader(
        SharedConfigPaths(
            instances=root / "instances",
            templates=root / "templates",
            site_file=root / "instances" / "site.yaml",
            devices_file=root / "instances" / "devices.yaml",
            coordination_root=root,
        )
    )


# ---------------------------------------------------------------------------
# 子进程入口（fork 上下文，继承仓库 src 导入路径）
# ---------------------------------------------------------------------------


def _hold_exclusive_lock(root_str: str, hold_seconds: float) -> None:
    descriptor = os.open(root_str, os.O_RDONLY | os.O_DIRECTORY)
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        time.sleep(hold_seconds)
    finally:
        os.close(descriptor)


def _commit_generations(root_str: str, generations: int) -> None:
    root = Path(root_str)
    devices_path = root / _DEVICES_REL
    units_path = root / _UNITS_REL
    import yaml

    for generation in range(1, generations + 1):
        devices = yaml.safe_load(devices_path.read_text(encoding="utf-8"))
        units = yaml.safe_load(units_path.read_text(encoding="utf-8"))
        devices["file_version"] = f"1.0.{generation}"
        units["file_version"] = f"1.0.{generation}"
        commit_config_files(
            root,
            {
                _DEVICES_REL: yaml.safe_dump(devices).encode(),
                _UNITS_REL: yaml.safe_dump(units).encode(),
            },
        )


def _reader_loop(
    root_str: str, deadline_seconds: float, queue: mp.Queue[tuple[str, object]]
) -> None:
    loader = _loader(Path(root_str))
    end = time.monotonic() + deadline_seconds
    observations = 0
    try:
        while time.monotonic() < end:
            documents = loader.load()
            devices_generation = tuple(
                int(part) for part in documents.devices_file_version.split(".")
            )
            units_generation = tuple(
                int(part) for part in str(documents.units["file_version"]).split(".")
            )
            # 跨文件代际必须一致：读方永远不能看到 devices/units 分属两个提交。
            assert devices_generation == units_generation, (
                devices_generation,
                units_generation,
            )
            observations += 1
    except BaseException as exc:  # 子进程内任何异常都回传主进程裁决
        queue.put(("error", repr(exc)))
        return
    queue.put(("ok", observations))


def _load_once(root_str: str, queue: mp.Queue[tuple[str, object]]) -> None:
    try:
        _loader(Path(root_str)).load()
    except ConfigError as exc:
        queue.put(("config_error", str(exc)))
    except BaseException as exc:
        queue.put(("other_error", repr(exc)))
    else:
        queue.put(("ok", ""))


def _recover_once(root_str: str, queue: mp.Queue[tuple[str, object]]) -> None:
    try:
        recover_config_commit(Path(root_str))
    except BaseException as exc:
        queue.put(("error", repr(exc)))
    else:
        queue.put(("ok", ""))


def _arm_interrupted_transaction(root_str: str, new_file_rel: str) -> None:
    """模拟 marker 已武装、部分文件已替换后的崩溃窗口并停留等待 SIGKILL。"""
    root = Path(root_str)
    transaction = uuid.uuid4().hex
    folder = root / _STORE / transaction
    devices = root / _DEVICES_REL
    original = devices.read_bytes()
    corrupted = original + b"# crashed mid-commit\n"
    new_file = root / new_file_rel
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_content = b"schema_version: 1\nfile_version: 1.0.0\n"

    (folder / "before" / _DEVICES_REL).parent.mkdir(parents=True)
    (folder / "after" / _DEVICES_REL).parent.mkdir(parents=True)
    (folder / "before" / _DEVICES_REL).write_bytes(original)
    (folder / "after" / _DEVICES_REL).write_bytes(corrupted)
    (folder / "after" / new_file_rel).write_bytes(new_content)
    mode = stat.S_IMODE(devices.stat().st_mode)
    entries = [
        {"path": _DEVICES_REL, "existed": True, "mode": mode},
        {"path": new_file_rel, "existed": False, "mode": None},
    ]
    (root / _MARKER).write_text(
        json.dumps({"transaction_id": transaction, "files": entries}),
        encoding="utf-8",
    )
    devices.write_bytes(corrupted)
    new_file.write_bytes(new_content)
    while True:  # 由主进程 SIGKILL 终止，模拟无清理崩溃
        time.sleep(60)


# ---------------------------------------------------------------------------
# S2-2：跨进程共享读锁 / 排他写锁互斥（走真实 Loader 锁路径）
# ---------------------------------------------------------------------------


def test_reader_loader_blocks_behind_foreign_exclusive_lock(tmp_path: Path) -> None:
    ctx = mp.get_context("fork")
    root = _copy_tree(tmp_path)
    writer = ctx.Process(target=_hold_exclusive_lock, args=(str(root), 3.0))
    writer.start()
    time.sleep(0.6)  # 等待写方实际持有 LOCK_EX
    try:
        loader = _loader(root)
        outcome: dict[str, object] = {}

        def do_load() -> None:
            outcome["revision"] = loader.load().revision

        reader = threading.Thread(target=do_load, daemon=True)
        reader.start()
        reader.join(timeout=1.5)
        assert reader.is_alive(), "reader must block on the foreign exclusive lock"

        writer.join(timeout=10)
        assert not writer.is_alive()
        reader.join(timeout=15)
        assert not reader.is_alive()
        assert isinstance(outcome["revision"], str)
        assert len(outcome["revision"]) == 64
    finally:
        if writer.is_alive():
            assert writer.pid is not None
            os.kill(writer.pid, signal.SIGKILL)
            writer.join(timeout=5)


# ---------------------------------------------------------------------------
# S2-3：多文件提交期间独立读进程观察不到中间状态
# ---------------------------------------------------------------------------


def test_concurrent_reader_never_observes_partial_multifile_commit(tmp_path: Path) -> None:
    ctx = mp.get_context("fork")
    root = _copy_tree(tmp_path)
    queue: mp.Queue[tuple[str, object]] = ctx.Queue()
    reader = ctx.Process(target=_reader_loop, args=(str(root), 6.0, queue))
    reader.start()
    time.sleep(0.5)
    try:
        _commit_generations(str(root), generations=40)
    finally:
        reader.join(timeout=20)
    assert not reader.is_alive(), "reader process did not converge"
    assert reader.exitcode == 0

    results = []
    while True:
        try:
            results.append(queue.get_nowait())
        except Empty:
            break
    assert results, "reader produced no observations"
    assert all(kind == "ok" for kind, _payload in results), results
    assert any(
        isinstance(observations, int) and observations > 0 for _kind, observations in results
    )

    # 提交结束后最终代际一致且可被新读者完整读取。
    final = _loader(root).load()
    assert final.devices_file_version == "1.0.40"
    assert str(final.units["file_version"]) == "1.0.40"


# ---------------------------------------------------------------------------
# S2-4/5：SIGKILL 后 marker 存活 -> 独立读者 fail-closed；
#         独立进程恢复完整文件集（含新增文件删除），恢复幂等
# ---------------------------------------------------------------------------


def test_sigkill_with_marker_fails_closed_then_process_recovery_restores(
    tmp_path: Path,
) -> None:
    ctx = mp.get_context("fork")
    root = _copy_tree(tmp_path)
    devices = root / _DEVICES_REL
    original = devices.read_bytes()
    new_file_rel = "instances/crash-added.yaml"

    victim = ctx.Process(target=_arm_interrupted_transaction, args=(str(root), new_file_rel))
    victim.start()
    marker = root / _MARKER
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    assert victim.pid is not None
    os.kill(victim.pid, signal.SIGKILL)
    victim.join(timeout=5)
    assert not victim.is_alive()

    # 独立读进程：存在事务标记时必须 fail-closed，不得解析半成品文件集。
    blocked: mp.Queue[tuple[str, object]] = ctx.Queue()
    blocked_reader = ctx.Process(target=_load_once, args=(str(root), blocked))
    blocked_reader.start()
    blocked_reader.join(timeout=15)
    assert blocked_reader.exitcode == 0
    kind, payload = blocked.get(timeout=5)
    assert kind == "config_error", (kind, payload)
    assert isinstance(payload, str) and "interrupted" in payload

    # 独立恢复进程：回滚已改文件、删除事务内新增文件。
    recovered: mp.Queue[tuple[str, object]] = ctx.Queue()
    recovery = ctx.Process(target=_recover_once, args=(str(root), recovered))
    recovery.start()
    recovery.join(timeout=15)
    assert recovery.exitcode == 0
    assert recovered.get(timeout=5) == ("ok", "")
    assert devices.read_bytes() == original
    assert not (root / new_file_rel).exists()
    assert not marker.exists()

    # 恢复幂等：再跑一个独立恢复进程仍是 no-op，随后读者成功。
    again: mp.Queue[tuple[str, object]] = ctx.Queue()
    second = ctx.Process(target=_recover_once, args=(str(root), again))
    second.start()
    second.join(timeout=15)
    assert second.exitcode == 0
    assert again.get(timeout=5) == ("ok", "")

    healthy: mp.Queue[tuple[str, object]] = ctx.Queue()
    healthy_reader = ctx.Process(target=_load_once, args=(str(root), healthy))
    healthy_reader.start()
    healthy_reader.join(timeout=15)
    assert healthy_reader.exitcode == 0
    assert healthy.get(timeout=5)[0] == "ok"
