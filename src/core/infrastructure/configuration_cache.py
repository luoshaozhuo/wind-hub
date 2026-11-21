"""Durable, content-addressed cache for verified Repository Service bundles."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from core.configuration.deployment import INSTANCE_ROLE_TYPES, service_instance_refs
from core.configuration.publication_identity import resolve_publication_identity
from core.infrastructure.config.generic_yaml import parse_yaml
from core.infrastructure.configuration_repository import SelectedBundle

_INSTANCE_PREFIXES = {
    "site": "site",
    "device": "device",
    "collector_tasks": "collector_tasks",
    "scenario": "scenarios",
    "sink": "sinks",
    "delivery": "deliveries",
}


def stage_selected_bundle(
    bundle: SelectedBundle, cache_root: Path, deployment_id: str, service_id: str
) -> Path:
    """Write one verified immutable closure outside the published configuration root.

    Returns:
        Local Service YAML path for an isolated, fully prepared cache generation.

    Raises:
        ValueError: Selected Service, Instance or cache storage is inconsistent.
    """
    cache_root = cache_root.absolute()
    if cache_root.is_symlink():
        raise ValueError("configuration cache root cannot be a symlink")
    cache_root.mkdir(parents=True, exist_ok=True)
    service_paths = [
        name for name in bundle.files if name.startswith("services/")
    ]
    if len(service_paths) != 1:
        raise ValueError("Repository bundle needs one Service document")
    service_path = service_paths[0]
    service_doc = parse_yaml(bundle.files[service_path].decode("utf-8"))
    service_identity = resolve_publication_identity(service_path, service_doc)
    if service_identity.config_type != "service" or service_identity.config_id != service_id:
        raise ValueError("Repository selected an unexpected Service")
    refs = service_instance_refs(service_doc, INSTANCE_ROLE_TYPES)
    selected: dict[str, dict[str, dict[str, str]]] = {}
    for kind, config_id in refs:
        prefix = _INSTANCE_PREFIXES[kind]
        candidates = [
            name for name in bundle.files
            if name.startswith(f"instances/{prefix}_{config_id}_v")
            and name.endswith(".yaml")
        ]
        if len(candidates) != 1:
            raise ValueError("Repository bundle lacks a unique selected Instance")
        path = candidates[0]
        document = parse_yaml(bundle.files[path].decode("utf-8"))
        instance = resolve_publication_identity(path, document)
        if instance.config_type != kind or instance.config_id != config_id:
            raise ValueError("Repository Instance identity mismatch")
        selected.setdefault(kind, {})[config_id] = {"file_version": instance.file_version}
    manifest = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "deployment_id": deployment_id,
        "services": {
            service_id: {
                "config_id": service_id, "file_version": service_identity.file_version
            }
        },
        "instances": selected,
    }
    destination = cache_root / bundle.target_revision
    expected_path = destination / "configs" / service_path
    if expected_path.is_file() and not expected_path.is_symlink():
        if expected_path.read_bytes() != bundle.files[service_path]:
            raise ValueError("cached Service identity has changed")
        return expected_path
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=cache_root))
    try:
        configs = stage / "configs"
        for name, data in sorted(bundle.files.items()):
            target = configs / name
            if not target.is_relative_to(configs):
                raise ValueError("unsafe Repository bundle path")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        manifests = configs / "deployments"
        manifests.mkdir(parents=True, exist_ok=True)
        manifest_path = manifests / f"deployment_{deployment_id}_v1.0.0.yaml"
        with manifest_path.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(manifest, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(stage, destination)
        return expected_path
    finally:
        if stage.exists():
            import shutil

            shutil.rmtree(stage)
