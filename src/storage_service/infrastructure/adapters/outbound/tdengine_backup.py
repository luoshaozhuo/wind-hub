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
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from storage_service.application.config import StorageConfig
from storage_service.domain.wide_schema import WideSchema
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ImmutableConfigArchive,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore

_SAFE_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")


class NativeBackupError(RuntimeError):
    """The native backup tool or its generated artifact cannot be trusted."""


class NativeTDengineBackup:
    """Invoke taosdump using an exact argv and controlled output directory."""

    def __init__(
        self,
        *,
        config: StorageConfig,
        metadata: MetadataStore,
        archive: ImmutableConfigArchive,
    ) -> None:
        self._config = config
        self._metadata = metadata
        self._archive = archive

    @staticmethod
    def _database_for_farm(wind_farm_id: str) -> str:
        """Use the one authoritative wide database naming algorithm."""
        return WideSchema.database_name(wind_farm_id)

    async def backup(
        self,
        *,
        task_id: str,
        wind_farm_id: str,
    ) -> tuple[str, int, str]:
        if not self._config.backup.enabled:
            raise NativeBackupError("native backup requires explicit verified enablement")
        if not _SAFE_IDENTIFIER.fullmatch(task_id):
            raise ValueError("backup task ID is not a generated UUID hex")
        database = self._database_for_farm(wind_farm_id)
        registrations = await asyncio.to_thread(
            self._metadata.list_ready_wide_tables, database=database
        )
        if not registrations:
            raise NativeBackupError("Storage has no verified device-wide tables for this farm")
        host, port = self._split_endpoint(self._config.tdengine.endpoint)
        password_name = self._config.tdengine.credential_ref.removeprefix("env:")
        password = os.getenv(password_name)
        username = os.getenv("WIND_HUB_TDENGINE_USER")
        if not password or not username:
            raise NativeBackupError("TDengine backup credentials are unavailable")
        binary = shutil.which("taosdump")
        if binary is None:
            raise NativeBackupError("TDengine taosdump utility is not installed")

        root = self._config.backup.root
        if root.is_symlink():
            raise NativeBackupError("backup root is a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / task_id
        path.mkdir(mode=0o700)
        # -Z 1 selects TDengine WebSocket on port 6041.
        # -p requests interactive authentication: never trust an undocumented
        # TDENGINE_PASSWORD fallback or expose secrets in argv/process lists.
        # Whether the target 3.3.6.13 taosdump accepts piped input is a
        # mandatory backup acceptance check; if not, fail closed.
        command = [
            binary,
            "-Z",
            "1",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            username,
            "-p",
            "-D",
            database,
            "-o",
            str(path),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        watcher = asyncio.create_task(self._watch_output(process, path))
        try:
            await asyncio.wait_for(
                process.communicate((password + "\n").encode("utf-8")),
                timeout=self._config.backup.timeout_s,
            )
            # Propagate a quota violation even if taosdump has already exited.
            if watcher.done() and not watcher.cancelled():
                watcher.result()
            result = process.returncode
        except BaseException:
            if process.returncode is None:
                process.kill()
            await asyncio.shield(process.wait())
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        if result is None:
            raise NativeBackupError("native backup exit state is unknown")
        if result != 0:
            raise NativeBackupError(f"TDengine taosdump returned a nonzero exit code ({result})")
        if await asyncio.to_thread(self._output_size, path, self._config.backup.max_bytes):
            raise NativeBackupError("native backup exceeded configured byte budget")

        # Native backup is not restorable without the immutable physical
        # schema dependency closure. Snapshot the content-addressed YAML first.
        archive_identities = await asyncio.to_thread(
            self._archive.snapshot_for_backup,
            destination=path / "configuration-archive",
            max_bytes=self._config.backup.max_bytes,
        )
        bindings = tuple(
            binding
            for binding in self._metadata.list_device_bindings()
            if binding.wind_farm_id == wind_farm_id
        )
        semantic = self._metadata.list_semantic_pins()
        required: set[tuple[str, str, str]] = set()
        if not bindings:
            raise NativeBackupError("historical device binding closure is unavailable")
        for binding in bindings:
            pin = semantic.get(binding.point_table_id)
            if pin is None:
                raise NativeBackupError("historical PointTable semantic pin is unavailable")
            required.update(
                (
                    ("device", binding.device_config_id, binding.device_version_id),
                    ("device_model", binding.model.config_id, binding.model.version_id),
                    ("point_table", pin[0][0], pin[0][1]),
                )
            )
            if pin[1] is not None:
                required.add(("business_points", pin[1][0], pin[1][1]))
        if not required.issubset(
            {
                (kind, config_id, version_id)
                for kind, config_id, version_id, _sha in archive_identities
            }
        ):
            # Archived files are addressed by Repository version_id, not
            # the human-readable file_version. Never replace with latest.
            raise NativeBackupError(
                "historical device/model/point-table closure is missing from backup"
            )
        if registrations != await asyncio.to_thread(
            self._metadata.list_ready_wide_tables, database=database
        ):
            raise NativeBackupError(
                "wide physical table ownership changed while backup was running"
            )
        current_bindings = tuple(
            binding
            for binding in self._metadata.list_device_bindings()
            if binding.wind_farm_id == wind_farm_id
        )
        if current_bindings != bindings or self._metadata.list_semantic_pins() != semantic:
            raise NativeBackupError(
                "historical configuration identity changed during native backup"
            )
        manifest, bytes_written, digest = await asyncio.to_thread(
            self._verify_backup,
            path,
            database,
        )
        if bytes_written > self._config.backup.max_bytes:
            raise NativeBackupError("native backup exceeded configured byte budget")
        manifest["historical_configuration"] = {
            "documents": [
                {
                    "config_type": kind,
                    "config_id": config_id,
                    "version_id": version,
                    "yaml_sha256": digest,
                }
                for kind, config_id, version, digest in archive_identities
            ],
            "device_bindings": [asdict(binding) for binding in bindings],
            "semantic_pins": {
                binding.point_table_id: {
                    "point_table": semantic[binding.point_table_id][0],
                    "business_points": semantic[binding.point_table_id][1],
                }
                for binding in bindings
            },
        }
        manifest["wide_schema_ownership"] = [
            {
                "name": item.name,
                "full_sha256": item.full_sha256,
                "schema_fingerprint": item.schema_fingerprint,
                "identity_json": item.identity_json,
            }
            for item in registrations
        ]
        # The manifest digest covers all physical files and ownership metadata,
        # excluding only its own digest field.
        manifest.pop("sha256", None)
        digest = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest["sha256"] = digest
        manifest_file = path / "storage_manifest.json"
        with manifest_file.open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        total_bytes = bytes_written + manifest_file.stat().st_size
        if total_bytes > self._config.backup.max_bytes:
            raise NativeBackupError("native backup exceeded configured byte budget")
        if await asyncio.to_thread(self._output_size, path, self._config.backup.max_bytes):
            raise NativeBackupError("native backup exceeded configured byte budget")
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        verified_size, verified_digest = await asyncio.to_thread(
            self.verify_manifest, path, database=database
        )
        if verified_size != total_bytes or verified_digest != digest:
            raise NativeBackupError("post-write backup manifest verification failed")
        return task_id, verified_size, verified_digest

    @staticmethod
    def verify_manifest(path: Path, *, database: str) -> tuple[int, str]:
        """Verify file bytes and historical YAML closure before permitting restore."""
        if path.is_symlink() or not path.is_dir():
            raise NativeBackupError("backup directory is not trusted")
        manifest_file = path / "storage_manifest.json"
        if manifest_file.is_symlink() or not manifest_file.is_file():
            raise NativeBackupError("native backup manifest is missing or unsafe")
        try:
            manifest = json.loads(manifest_file.read_bytes())
        except (OSError, ValueError, TypeError) as exc:
            raise NativeBackupError("native backup manifest is malformed") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("database") != database
            or manifest.get("manifest_schema_version") != 1
        ):
            raise NativeBackupError("native backup manifest identity mismatch")
        claimed = manifest.get("sha256")
        if not isinstance(claimed, str) or re.fullmatch(r"[0-9a-f]{64}", claimed) is None:
            raise NativeBackupError("native backup digest is invalid")
        unsigned = {key: value for key, value in manifest.items() if key != "sha256"}
        actual = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if actual != claimed:
            raise NativeBackupError("native backup manifest digest mismatch")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise NativeBackupError("native backup file index missing")
        listed: set[str] = set()
        total = 0
        for item in entries:
            if (
                not isinstance(item, list)
                or len(item) != 3
                or not isinstance(item[0], str)
                or type(item[1]) is not int
                or item[1] < 0
                or not isinstance(item[2], str)
                or re.fullmatch(r"[0-9a-f]{64}", item[2]) is None
            ):
                raise NativeBackupError("native backup file entry malformed")
            name, size, digest = item
            pure = Path(name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or name == "storage_manifest.json"
                or not pure.parts
                or name in listed
            ):
                raise NativeBackupError("native backup file path invalid")
            listed.add(name)
            file = path / name
            if file.is_symlink() or not file.is_file():
                raise NativeBackupError("native backup file missing or unsafe")
            with file.open("rb") as stream:
                actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if file.stat().st_size != size or actual_digest != digest:
                raise NativeBackupError("native backup file hash or size mismatch")
            total += size
        if total != manifest.get("total_bytes"):
            raise NativeBackupError("native backup total byte count mismatch")
        actual_files: set[str] = set()
        for file in path.rglob("*"):
            if file.is_symlink() or not (file.is_dir() or file.is_file()):
                raise NativeBackupError("native backup contains unsafe file")
            if file.is_file():
                actual_files.add(file.relative_to(path).as_posix())
        if actual_files != listed | {"storage_manifest.json"}:
            raise NativeBackupError("native backup file index is incomplete")
        history = manifest.get("historical_configuration")
        if not isinstance(history, dict) or not isinstance(history.get("documents"), list):
            raise NativeBackupError("historical configuration backup index missing")
        if not isinstance(history.get("device_bindings"), list) or not isinstance(
            history.get("semantic_pins"), dict
        ):
            raise NativeBackupError("historical configuration bindings missing")
        for ref in history["documents"]:
            if not isinstance(ref, dict) or set(ref) != {
                "config_type",
                "config_id",
                "version_id",
                "yaml_sha256",
            }:
                raise NativeBackupError("historical configuration entry malformed")
            values = tuple(ref.values())
            if any(not isinstance(value, str) or not value for value in values):
                raise NativeBackupError("historical configuration identity invalid")
            identity = json.dumps(
                [
                    ref["config_type"],
                    ref["config_id"],
                    ref["version_id"],
                    ref["yaml_sha256"],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            folder = hashlib.sha256(identity).hexdigest()
            yaml_path = f"configuration-archive/{folder}/document.yaml"
            metadata_path = f"configuration-archive/{folder}/manifest.json"
            if yaml_path not in listed or metadata_path not in listed:
                raise NativeBackupError("historical configuration files unavailable")
            if hashlib.sha256((path / yaml_path).read_bytes()).hexdigest() != ref["yaml_sha256"]:
                raise NativeBackupError("historical YAML content digest mismatch")
        ownership = manifest.get("wide_schema_ownership")
        if not isinstance(ownership, list) or not ownership:
            raise NativeBackupError("wide physical ownership index missing")
        for obj in ownership:
            if not isinstance(obj, dict) or set(obj) != {
                "name",
                "full_sha256",
                "schema_fingerprint",
                "identity_json",
            }:
                raise NativeBackupError("wide physical ownership entry malformed")
            if (
                not isinstance(obj["name"], str)
                or not obj["name"].startswith(database + ".")
                or hashlib.sha256(obj["identity_json"].encode("utf-8")).hexdigest()
                != obj["full_sha256"]
            ):
                raise NativeBackupError("wide physical ownership digest mismatch")
        return total + manifest_file.stat().st_size, actual

    @staticmethod
    def _output_size(path: Path, limit: int) -> bool:
        """Report an exceeded quota without following backup-created links."""
        used = 0
        for entry in path.rglob("*"):
            if entry.is_symlink() or (not entry.is_file() and not entry.is_dir()):
                raise NativeBackupError("native backup created an unsafe output entry")
            if entry.is_file():
                used += entry.stat().st_size
                if used > limit:
                    return True
        return False

    async def _watch_output(self, process: asyncio.subprocess.Process, path: Path) -> None:
        """Stop taosdump while it runs, not after it has exhausted the disk."""
        while process.returncode is None:
            if await asyncio.to_thread(self._output_size, path, self._config.backup.max_bytes):
                if process.returncode is None:
                    process.kill()
                raise NativeBackupError("native backup exceeded configured byte budget")
            await asyncio.sleep(0.2)

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
                raise NativeBackupError("native backup contains symbolic links")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise NativeBackupError("native backup contains special files")
            relative = candidate.relative_to(path).as_posix()
            if not relative or relative.startswith("../"):
                raise NativeBackupError("invalid native backup output path")
            if candidate.suffix == ".avro":
                avro_files += 1
            with candidate.open("rb") as stream:
                fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
            size = candidate.stat().st_size
            total += size
            files.append([relative, size, fingerprint])
        if not files or total <= 0 or avro_files == 0:
            raise NativeBackupError("taosdump output contains no verified AVRO backup payload")
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
