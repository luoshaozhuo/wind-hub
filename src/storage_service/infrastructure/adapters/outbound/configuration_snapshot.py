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
from pathlib import Path
from typing import Any

from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import ConfigHistoryUnavailable
from storage_service.infrastructure.adapters.outbound.configuration_grpc import StorageBundle


class StorageBundleSnapshot:
    """Atomic CURRENT replacement and SHA-256 verification with no hot deletion."""

    def __init__(self, directory: Path) -> None:
        if not directory.is_absolute():
            raise ValueError("Configuration snapshot root must be absolute")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._path = directory / "accepted_bundle.json"

    @staticmethod
    def _encode(bundle: StorageBundle) -> bytes:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "revision_id": bundle.revision_id,
            "config": bundle.configuration.model_dump(mode="json"),
            "point_table_versions": {
                key: list(value) for key, value in sorted(bundle.point_table_versions.items())
            },
            "business_points_pin": (
                list(bundle.business_points_pin)
                if bundle.business_points_pin is not None else None
            ),
        }
        canonical = json.dumps(
            payload, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode()
        envelope = {
            "payload": payload,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }
        return json.dumps(
            envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()

    def store(self, bundle: StorageBundle) -> None:
        if bundle.revision_id <= 0 or not bundle.point_table_versions:
            raise ConfigHistoryUnavailable("cannot persist an incomplete Storage snapshot")
        content = self._encode(bundle)
        staging: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self._path.parent, prefix=".bundle-", delete=False
            ) as stream:
                staging = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, self._path)
            staging = None
            parent_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if staging is not None:
                os.unlink(staging)

    def load(self) -> StorageBundle:
        """Reject tampered/partial snapshots and incompatible schema versions."""
        try:
            if self._path.is_symlink() or self._path.stat().st_size > 1024 * 1024:
                raise ConfigHistoryUnavailable("Configuration snapshot path is unsafe")
            encoded = self._path.read_bytes()
            envelope = json.loads(encoded)
            payload = envelope["payload"]
            canonical = json.dumps(
                payload, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False,
            ).encode()
            if not hashlib.sha256(canonical).hexdigest() == envelope["sha256"]:
                raise ConfigHistoryUnavailable("Configuration snapshot SHA256 mismatch")
            if payload["schema_version"] != 1 or type(payload["revision_id"]) is not int:
                raise ConfigHistoryUnavailable("Configuration snapshot format unsupported")
            pins = payload["point_table_versions"]
            if not isinstance(pins, dict) or not pins:
                raise ConfigHistoryUnavailable("Configuration snapshot has no point tables")
            converted: dict[str, tuple[str, str]] = {}
            for point_id, value in pins.items():
                if (
                    not isinstance(point_id, str) or not isinstance(value, list)
                    or len(value) != 2 or any(not isinstance(x, str) or not x for x in value)
                ):
                    raise ConfigHistoryUnavailable("invalid historical PointTable pin")
                converted[point_id] = (value[0], value[1])
            business = payload["business_points_pin"]
            if business is not None and (
                not isinstance(business, list) or len(business) != 2
                or any(not isinstance(x, str) or not x for x in business)
            ):
                raise ConfigHistoryUnavailable("invalid BusinessPoint pin")
            return StorageBundle(
                configuration=StorageConfig.model_validate(payload["config"]),
                revision_id=payload["revision_id"],
                point_table_versions=converted,
                business_points_pin=(business[0], business[1]) if business else None,
            )
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ConfigHistoryUnavailable(
                "no complete trusted local Configuration snapshot is available"
            ) from exc
