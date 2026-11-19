"""TDengine native taosdump backup: bounded subprocess, no argv secrets.

The target TDengine 3.3.6.13 tool must pass an actual backup/restore Spike before
enabling this path in production. It never substitutes a CSV file for a backup.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from storage_service.application.config import StorageConfig
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore

_SAFE_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")


class NativeBackupFailure(RuntimeError):
    """The native backup tool or its generated artifact cannot be trusted."""


class NativeTDengineBackup:
    """Invoke taosdump using an exact argv and controlled output directory."""

    def __init__(self, *, config: StorageConfig, metadata: MetadataStore) -> None:
        self._config = config
        self._metadata = metadata

    @staticmethod
    def _database_for_farm(wind_farm_id: str) -> str:
        if not wind_farm_id:
            raise ValueError("backup requires a nonempty wind farm ID")
        digest = hashlib.sha256(
            json.dumps([wind_farm_id], ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"wh_{digest[:24]}"

    async def backup(
        self, *, task_id: str, wind_farm_id: str,
    ) -> tuple[str, int, str]:
        if not self._config.backup.enabled:
            raise NativeBackupFailure("native backup requires explicit verified enablement")
        if not _SAFE_IDENTIFIER.fullmatch(task_id):
            raise ValueError("backup task ID is not a generated UUID hex")
        database = self._database_for_farm(wind_farm_id)
        if not await asyncio.to_thread(self._metadata.list_tables, database=database):
            raise NativeBackupFailure("Storage has no registered tables for this farm")
        host, port = self._split_endpoint(self._config.tdengine.endpoint)
        password_name = self._config.tdengine.credential_ref.removeprefix("env:")
        password = os.getenv(password_name)
        username = os.getenv("WIND_HUB_TDENGINE_USER")
        if not password or not username:
            raise NativeBackupFailure("TDengine backup credentials are unavailable")
        binary = shutil.which("taosdump")
        if binary is None:
            raise NativeBackupFailure("TDengine taosdump utility is not installed")

        root = self._config.backup.root
        if root.is_symlink():
            raise NativeBackupFailure("backup root is a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / task_id
        path.mkdir(mode=0o700)
        # Credentials reside only in the child environment; no passwords in argv.
        environment = {
            **os.environ, "TDENGINE_USER": username,
            "TDENGINE_PASSWORD": password,
        }
        command = [
            binary, "-Z", "1", "-h", host, "-P", str(port),
            "-D", database, "-o", str(path),
        ]
        process = await asyncio.create_subprocess_exec(
            *command, env=environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            result = await asyncio.wait_for(
                process.wait(), timeout=self._config.backup.timeout_s,
            )
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await asyncio.shield(process.wait())
            raise
        if result != 0:
            raise NativeBackupFailure(
                f"TDengine taosdump returned a nonzero exit code ({result})"
            )

        manifest, bytes_written, digest = await asyncio.to_thread(
            self._verify_backup, path, database,
        )
        if bytes_written > self._config.backup.max_bytes:
            raise NativeBackupFailure("native backup exceeded configured byte budget")
        manifest_file = path / "storage_manifest.json"
        with manifest_file.open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return task_id, bytes_written, digest

    @staticmethod
    def _split_endpoint(address: str) -> tuple[str, int]:
        match = re.fullmatch(r"([A-Za-z0-9.-]+):([0-9]{1,5})", address)
        if match is None:
            raise ValueError("TDengine endpoint must be host:port")
        port = int(match.group(2))
        if not 1 <= port <= 65535:
            raise ValueError("invalid TDengine endpoint port")
        return match.group(1), port

    @staticmethod
    def _verify_backup(path: Path, database: str) -> tuple[dict[str, object], int, str]:
        """Hash every generated file and reject links, empty/ambiguous backups."""
        files: list[list[object]] = []
        total = 0
        avro_files = 0
        for candidate in sorted(path.rglob("*")):
            if candidate.is_symlink():
                raise NativeBackupFailure("native backup contains symbolic links")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise NativeBackupFailure("native backup contains special files")
            relative = candidate.relative_to(path).as_posix()
            if not relative or relative.startswith("../"):
                raise NativeBackupFailure("invalid native backup output path")
            if candidate.suffix == ".avro":
                avro_files += 1
            with candidate.open("rb") as stream:
                fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
            size = candidate.stat().st_size
            total += size
            files.append([relative, size, fingerprint])
        if not files or total <= 0 or avro_files == 0:
            raise NativeBackupFailure(
                "taosdump output contains no verified AVRO backup payload"
            )
        manifest: dict[str, object] = {
            "manifest_schema_version": 1,
            "database": database,
            "files": files,
            "total_bytes": total,
        }
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest["sha256"] = digest
        return manifest, total, digest
