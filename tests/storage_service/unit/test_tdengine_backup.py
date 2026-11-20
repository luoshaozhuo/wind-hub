"""taosdump 真实子进程的配额、取消、超时和正常退出清理。"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.infrastructure.adapters.outbound.tdengine_backup import (
    NativeBackupError,
    NativeTDengineBackup,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit

_TAOSDUMP = r"""#!/usr/bin/env python3
import os
import pathlib
import sys
import time

mode = os.environ["TEST_TAOSDUMP_MODE"]
out = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
sys.stdin.readline()
pathlib.Path(os.environ["TEST_TAOSDUMP_PID_FILE"]).write_text(
    str(os.getpid()), encoding="ascii"
)
if mode == "normal":
    (out / "data.avro").write_bytes(b"verified-avro-payload")
    raise SystemExit(0)
if mode == "exceed":
    (out / "data.avro").write_bytes(b"x" * (1024 * 1024 + 1))
while True:
    time.sleep(60)
"""


class _Metadata:
    def list_tables(self, *, database: str) -> list[object]:
        return [object()]


def _config(tmp_path: Path) -> StorageConfig:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage_v1.0.1.yaml").read_bytes()
    )
    return config.model_copy(
        update={
            "backup": config.backup.model_copy(
                update={
                    "enabled": True,
                    "root": tmp_path / "backups",
                    "max_bytes": 1024 * 1024,
                    "timeout_s": 30,
                }
            )
        }
    )


def _backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
) -> tuple[NativeTDengineBackup, Path]:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    executable = executable_dir / "taosdump"
    executable.write_text(_TAOSDUMP, encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{executable_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("TEST_TAOSDUMP_MODE", mode)
    monkeypatch.setenv("TEST_TAOSDUMP_PID_FILE", str(tmp_path / "child.pid"))
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    config = _config(tmp_path)
    monkeypatch.setenv(config.tdengine.credential_ref.removeprefix("env:"), "taosdata")
    return (
        NativeTDengineBackup(config=config, metadata=cast(Any, _Metadata())),
        tmp_path / "backups",
    )


def _read_pid(root: Path, task_id: str) -> int:
    return int((root.parent / "child.pid").read_text(encoding="ascii"))


def _assert_reaped(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def _wait_for_pid(root: Path, task_id: str) -> int:
    for _ in range(200):
        try:
            return _read_pid(root, task_id)
        except FileNotFoundError:
            await asyncio.sleep(0.01)
    raise AssertionError("taosdump test child did not start")


async def test_normal_backup_reaps_taosdump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="normal")
    task_id = uuid.uuid4().hex
    artifact, size, digest = await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    assert artifact == task_id
    manifest_size = (root / task_id / "storage_manifest.json").stat().st_size
    assert size == len(b"verified-avro-payload") + manifest_size
    assert len(digest) == 64
    assert (root / task_id / "storage_manifest.json").is_file()
    _assert_reaped(_read_pid(root, task_id))


async def test_overquota_kills_running_taosdump_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="exceed")
    task_id = uuid.uuid4().hex
    started = time.monotonic()
    with pytest.raises(NativeBackupError, match="byte budget"):
        await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    assert time.monotonic() - started < 10
    _assert_reaped(_read_pid(root, task_id))


async def test_cancellation_kills_and_reaps_taosdump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="hang")
    task_id = uuid.uuid4().hex
    running = asyncio.create_task(backup.backup(task_id=task_id, wind_farm_id="farm-1"))
    pid = await _wait_for_pid(root, task_id)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    _assert_reaped(pid)


async def test_timeout_kills_and_reaps_taosdump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="hang")
    task_id = uuid.uuid4().hex
    real_wait_for = asyncio.wait_for

    async def short_timeout(awaitable: Any, timeout: float) -> Any:
        return await real_wait_for(awaitable, 0.2 if timeout == 30 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_timeout)
    with pytest.raises(TimeoutError):
        await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    _assert_reaped(_read_pid(root, task_id))
