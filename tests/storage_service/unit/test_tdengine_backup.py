"""taosdump 真实子进程的配额、取消、超时和正常退出清理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from storage_service.application.config import (
    DeviceBinding,
    DeviceModel,
    StorageConfig,
    load_storage_config,
)
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ImmutableConfigArchive,
    RepositoryDocument,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import ObjectRegistration
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
time.sleep(0.05)
sys.stderr.write("Enter password: ")
sys.stderr.flush()
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
    def list_ready_wide_tables(self, *, database: str) -> tuple[ObjectRegistration, ...]:
        identity_json = '["farm-1","wtg-001","point_table_wind_v1.0.0",' '"1.0.0","protocol","fp"]'
        return (
            ObjectRegistration(
                name=f"{database}.tb_wtg_0123456789abcdef",
                full_sha256=hashlib.sha256(identity_json.encode()).hexdigest(),
                identity_json=identity_json,
                schema_fingerprint="fp",
                state="READY",
            ),
        )

    def list_device_bindings(self) -> tuple[DeviceBinding, ...]:
        return (
            DeviceBinding(
                wind_farm_id="farm-1",
                device_id="wtg-001",
                device_config_id="farm-a",
                device_version_id="device-repo-v1",
                device_file_version="1.0.0",
                site_config_id="",
                site_file_version="",
                model=DeviceModel(
                    config_id="wind",
                    version_id="model-repo-v1",
                    file_version="1.0.0",
                    device_type_id="wind",
                    point_table_id="point_table_wind_v1.0.0",
                ),
            ),
        )

    def list_semantic_pins(
        self,
    ) -> dict[str, tuple[tuple[str, str], tuple[str, str] | None]]:
        return {
            "point_table_wind_v1.0.0": (
                ("wind", "table-repo-v1"),
                None,
            )
        }


def _config(tmp_path: Path) -> StorageConfig:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
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
    archive = ImmutableConfigArchive(tmp_path / "archive")
    for kind, name, version in (
        ("device", "farm-a", "device-repo-v1"),
        ("device_model", "wind", "model-repo-v1"),
        ("point_table", "wind", "table-repo-v1"),
    ):
        body = b"schema_version: 1\nfile_version: 1.0.0\n"
        archive.persist(
            RepositoryDocument(
                config_type=kind,
                config_id=name,
                version_id=version,
                revision_id=1,
                yaml_bytes=body,
                yaml_sha256=hashlib.sha256(body).hexdigest(),
            )
        )
    return (
        NativeTDengineBackup(config=config, metadata=cast(Any, _Metadata()), archive=archive),
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
        except (FileNotFoundError, ValueError):
            # The child creates the PID file before writing its contents.
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
    assert size > len(b"verified-avro-payload") + manifest_size
    manifest = json.loads((root / task_id / "storage_manifest.json").read_text())
    expected_digest = manifest.pop("sha256")
    assert expected_digest == digest
    assert (
        hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        == digest
    )
    assert len(manifest["historical_configuration"]["documents"]) == 3
    assert len(manifest["wide_schema_ownership"]) == 1
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


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("mode", ["normal", "hang"])
async def test_pty_fds_are_closed_after_success_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode=mode)
    task_id = uuid.uuid4().hex
    before = _fd_count()
    if mode == "normal":
        await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    else:
        running = asyncio.create_task(backup.backup(task_id=task_id, wind_farm_id="farm-1"))
        await _wait_for_pid(root, task_id)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert _fd_count() == before


async def test_pty_fds_are_closed_when_subprocess_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, _root = _backup(tmp_path, monkeypatch, mode="normal")

    async def fail_start(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected subprocess start failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_start)
    before = _fd_count()
    with pytest.raises(OSError, match="injected subprocess start failure"):
        await backup.backup(task_id=uuid.uuid4().hex, wind_farm_id="farm-1")
    assert _fd_count() == before


async def test_backup_manifest_verifier_rejects_avro_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="normal")
    task_id = uuid.uuid4().hex
    _, _, digest = await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    path = root / task_id
    assert (
        NativeTDengineBackup.verify_manifest(path, database=backup._database_for_farm("farm-1"))[1]
        == digest
    )
    (path / "data.avro").write_bytes(b"tampered")
    with pytest.raises(NativeBackupError, match="hash or size"):
        NativeTDengineBackup.verify_manifest(path, database=backup._database_for_farm("farm-1"))


async def test_backup_manifest_verifier_rejects_missing_historical_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="normal")
    task_id = uuid.uuid4().hex
    await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    path = root / task_id
    history = next((path / "configuration-archive").glob("*/document.yaml"))
    history.unlink()
    with pytest.raises(NativeBackupError, match="missing or unsafe"):
        NativeTDengineBackup.verify_manifest(path, database=backup._database_for_farm("farm-1"))


async def test_backup_rejects_ownership_registry_change_during_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, _root = _backup(tmp_path, monkeypatch, mode="normal")
    metadata = backup._metadata
    original = metadata.list_ready_wide_tables
    calls = 0

    def changed(*, database: str) -> tuple[ObjectRegistration, ...]:
        nonlocal calls
        calls += 1
        return original(database=database) if calls == 1 else ()

    monkeypatch.setattr(metadata, "list_ready_wide_tables", changed)
    with pytest.raises(NativeBackupError, match="ownership changed"):
        await backup.backup(task_id=uuid.uuid4().hex, wind_farm_id="farm-1")


async def test_backup_manifest_verifier_rejects_metadata_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup, root = _backup(tmp_path, monkeypatch, mode="normal")
    task_id = uuid.uuid4().hex
    await backup.backup(task_id=task_id, wind_farm_id="farm-1")
    path = root / task_id
    manifest_file = path / "storage_manifest.json"
    data = json.loads(manifest_file.read_text())
    data["wide_schema_ownership"][0]["schema_fingerprint"] = "other-schema"
    manifest_file.write_text(json.dumps(data))
    with pytest.raises(NativeBackupError, match="digest mismatch"):
        NativeTDengineBackup.verify_manifest(path, database=backup._database_for_farm("farm-1"))
