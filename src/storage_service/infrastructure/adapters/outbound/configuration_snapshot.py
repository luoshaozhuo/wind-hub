"""Crash-safe latest accepted Configuration closure snapshot.

The immutable YAML archive keeps exact document versions. This small manifest
records which verified revision and immutable PointTable pins were active
before shutdown; never invent a "latest" version during a repository outage.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from storage_service.application.config import (
    DeviceBinding,
    DeviceClosurePins,
    DeviceModel,
    StorageConfig,
)
from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_grpc import StorageBundle


class StorageBundleSnapshot:
    """Atomic CURRENT replacement and SHA-256 verification with no hot deletion."""

    def __init__(self, directory: Path) -> None:
        if not directory.is_absolute():
            raise ValueError("Configuration snapshot root must be absolute")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._path = directory / "accepted_bundle.json"
        self._pending_path = directory / "target_bundle.json"

    @staticmethod
    def _encode(bundle: StorageBundle) -> bytes:
        payload: dict[str, Any] = {
            "schema_version": 3,
            "revision_id": bundle.revision_id,
            "deployment_id": bundle.deployment_id,
            "target_revision": bundle.target_revision,
            "config": bundle.configuration.model_dump(mode="json"),
            "device_bindings": [asdict(binding) for binding in bundle.device_bindings],
            "point_table_versions": {
                key: list(value) for key, value in sorted(bundle.point_table_versions.items())
            },
            "business_points_pin": (
                list(bundle.business_points_pin) if bundle.business_points_pin is not None else None
            ),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        envelope = {
            "payload": payload,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }
        return json.dumps(
            envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()

    def store(self, bundle: StorageBundle) -> None:
        """Persist only a fully applied active closure."""
        self._store_at(self._path, bundle)

    def store_target(self, bundle: StorageBundle) -> None:
        """Persist a validated restart-required target without activating it."""
        self._store_at(self._pending_path, bundle)

    def load_target(self) -> StorageBundle:
        return self._load_at(self._pending_path)

    def _store_at(self, path: Path, bundle: StorageBundle) -> None:
        if bundle.revision_id <= 0 or not bundle.point_table_versions or not bundle.device_bindings:
            raise ConfigHistoryUnavailableError("cannot persist an incomplete Storage snapshot")
        content = self._encode(bundle)
        if path.exists():
            previous = self._load_at(path)
            if previous.revision_id > bundle.revision_id:
                raise ConfigHistoryUnavailableError(
                    "refusing to replace accepted Configuration revision with an older one"
                )
            if previous.revision_id == bundle.revision_id:
                if path.read_bytes() != content:
                    raise ConfigHistoryUnavailableError(
                        "same Configuration revision has changed its immutable closure"
                    )
                return
        staging: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".bundle-", delete=False
            ) as stream:
                staging = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, path)
            staging = None
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if staging is not None:
                os.unlink(staging)

    def load(self) -> StorageBundle:
        """Restore only the previous fully activated generation."""
        return self._load_at(self._path)

    def _load_at(self, path: Path) -> StorageBundle:
        """Reject tampered/partial snapshots and incompatible schema versions."""
        try:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ConfigHistoryUnavailableError("Configuration snapshot path is unsafe")
            encoded = path.read_bytes()
            envelope = json.loads(encoded)
            payload = envelope["payload"]
            canonical = json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            if not hashlib.sha256(canonical).hexdigest() == envelope["sha256"]:
                raise ConfigHistoryUnavailableError("Configuration snapshot SHA256 mismatch")
            if payload["schema_version"] != 3 or type(payload["revision_id"]) is not int:
                raise ConfigHistoryUnavailableError("Configuration snapshot format unsupported")
            if (
                not isinstance(payload["deployment_id"], str)
                or not payload["deployment_id"]
                or not isinstance(payload["target_revision"], str)
                or len(payload["target_revision"]) != 64
                or any(ch not in "0123456789abcdef" for ch in payload["target_revision"])
            ):
                raise ConfigHistoryUnavailableError("invalid persisted closure identity")
            pins = payload["point_table_versions"]
            if not isinstance(pins, dict) or not pins:
                raise ConfigHistoryUnavailableError("Configuration snapshot has no point tables")
            converted: dict[str, tuple[str, str]] = {}
            for point_id, value in pins.items():
                if (
                    not isinstance(point_id, str)
                    or not isinstance(value, list)
                    or len(value) != 2
                    or any(not isinstance(x, str) or not x for x in value)
                ):
                    raise ConfigHistoryUnavailableError("invalid historical PointTable pin")
                converted[point_id] = (value[0], value[1])
            business = payload["business_points_pin"]
            if business is not None and (
                not isinstance(business, list)
                or len(business) != 2
                or any(not isinstance(x, str) or not x for x in business)
            ):
                raise ConfigHistoryUnavailableError("invalid BusinessPoint pin")
            encoded_bindings = payload["device_bindings"]
            if not isinstance(encoded_bindings, list) or not encoded_bindings:
                raise ConfigHistoryUnavailableError("Configuration snapshot has no device bindings")
            bindings: list[DeviceBinding] = []
            for item in encoded_bindings:
                if not isinstance(item, dict) or not isinstance(item.get("model"), dict):
                    raise ConfigHistoryUnavailableError("invalid device instance binding")
                model = DeviceModel(**item["model"])
                binding = DeviceBinding(
                    wind_farm_id=item["wind_farm_id"],
                    device_id=item["device_id"],
                    device_config_id=item["device_config_id"],
                    device_version_id=item["device_version_id"],
                    device_file_version=item["device_file_version"],
                    site_config_id=item["site_config_id"],
                    site_file_version=item["site_file_version"],
                    model=model,
                )
                if binding.point_table_id not in converted:
                    raise ConfigHistoryUnavailableError(
                        "device binding references an unpinned PointTable"
                    )
                bindings.append(binding)
            return StorageBundle(
                configuration=StorageConfig.model_validate(payload["config"]),
                revision_id=payload["revision_id"],
                deployment_id=payload["deployment_id"],
                target_revision=payload["target_revision"],
                point_table_versions=DeviceClosurePins(converted, tuple(bindings)),
                business_points_pin=(business[0], business[1]) if business else None,
                device_bindings=tuple(bindings),
            )
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ConfigHistoryUnavailableError(
                "no complete trusted local Configuration snapshot is available"
            ) from exc
