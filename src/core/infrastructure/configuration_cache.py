"""Durable, content-addressed cache for verified Repository Service bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from core.configuration.deployment import (
    INSTANCE_ROLE_TYPES,
    closure_sha256,
    service_instance_refs,
)
from core.configuration.publication_identity import resolve_publication_identity
from core.infrastructure.config.generic_yaml import parse_yaml
from core.infrastructure.configuration_repository import SelectedBundle, _published_yaml

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
    service_paths = [name for name in bundle.files if name.startswith("services/")]
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
            name
            for name in bundle.files
            if name.startswith(f"instances/{prefix}_{config_id}_v") and name.endswith(".yaml")
        ]
        if len(candidates) != 1:
            raise ValueError("Repository bundle lacks a unique selected Instance")
        path = candidates[0]
        document = parse_yaml(bundle.files[path].decode("utf-8"))
        instance = resolve_publication_identity(path, document)
        if instance.config_type != kind or instance.config_id != config_id:
            raise ValueError("Repository Instance identity mismatch")
        selected.setdefault(kind, {})[config_id] = {"file_version": instance.file_version}
    # This synthesized local selector must match the process's explicit
    # bootstrap Deployment version; it is not a new Repository publication.
    manifest_version = os.environ.get("WIND_HUB_DEPLOYMENT_VERSION", "1.0.0")
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
        manifest_version,
    ):
        raise ValueError("invalid cached Deployment version")
    manifest = {
        "schema_version": 1,
        "file_version": manifest_version,
        "deployment_id": deployment_id,
        "services": {
            service_id: {"config_id": service_id, "file_version": service_identity.file_version}
        },
        "instances": selected,
    }
    version_root = cache_root / manifest_version
    if version_root.is_symlink():
        raise ValueError("cached Deployment selector root cannot be a symlink")
    version_root.mkdir(parents=True, exist_ok=True)
    destination = version_root / bundle.target_revision
    expected_path = destination / "configs" / service_path
    if destination.is_symlink():
        raise ValueError("cached Service generation cannot be a symlink")
    if destination.exists():
        _verify_cached_files(destination, bundle.files)
        _verify_cached_manifest(
            destination, deployment_id, manifest_version, manifest
        )
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
        manifest_path = manifests / f"deployment_{deployment_id}_v{manifest_version}.yaml"
        with manifest_path.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(manifest, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(stage, destination)
        return expected_path
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _verify_cached_manifest(
    destination: Path,
    deployment_id: str,
    version: str,
    expected: dict[str, object],
) -> None:
    path = (
        destination / "configs" / "deployments"
        / f"deployment_{deployment_id}_v{version}.yaml"
    )
    if not path.is_file() or path.is_symlink():
        raise ValueError("cached Deployment selector is missing or unsafe")
    document = yaml.safe_load(path.read_bytes())
    if document != expected:
        raise ValueError("cached Deployment selector has been modified")


def _verify_cached_files(destination: Path, expected: dict[str, bytes]) -> None:
    """Never trust an existing generation based on its Service file alone."""
    configs = destination / "configs"
    if destination.is_symlink() or configs.is_symlink():
        raise ValueError("configuration cache contains a symlink")
    actual: set[str] = set()
    for node in configs.rglob("*"):
        if node.is_symlink():
            raise ValueError("configuration cache contains a symlink")
        if node.is_file() and not node.is_relative_to(configs / "deployments"):
            actual.add(node.relative_to(configs).as_posix())
    if actual != set(expected):
        raise ValueError("cached configuration closure differs from Repository selection")
    for name, content in expected.items():
        file = configs / name
        if not file.is_file() or file.is_symlink() or file.read_bytes() != content:
            raise ValueError("cached configuration closure has been modified")


def _active_pointer(cache_root: Path, deployment_id: str, service_id: str) -> Path:
    identity = hashlib.sha256(
        json.dumps([deployment_id, service_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return cache_root / f"active-{identity}.json"


def mark_active_bundle(
    cache_root: Path, deployment_id: str, service_id: str, bundle: SelectedBundle
) -> None:
    """Atomically record only an activated, locally verifiable generation."""
    cache_root = cache_root.absolute()
    version = os.environ.get("WIND_HUB_DEPLOYMENT_VERSION", "1.0.0")
    generation = cache_root / version / bundle.target_revision
    stage_selected_bundle(bundle, cache_root, deployment_id, service_id)
    pointer = _active_pointer(cache_root, deployment_id, service_id)
    payload = {
        "revision": bundle.target_revision,
        "repository_revision": bundle.repository_revision,
        "deployment_version": version,
    }
    fd, temporary = tempfile.mkstemp(prefix=".active-", dir=cache_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, pointer)
        directory_fd = os.open(cache_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_active_bundle(
    cache_root: Path, deployment_id: str, service_id: str
) -> tuple[Path, SelectedBundle]:
    """Recover the previous active closure only if every document hashes correctly."""
    cache_root = cache_root.absolute()
    if cache_root.is_symlink():
        raise ValueError("configuration cache root is a symlink")
    pointer = _active_pointer(cache_root, deployment_id, service_id)
    if pointer.is_symlink():
        raise ValueError("configuration active pointer is a symlink")
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    version = payload["deployment_version"]
    revision = payload["revision"]
    repository_revision = payload["repository_revision"]
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        or not isinstance(revision, str)
        or not re.fullmatch(r"[a-f0-9]{64}", revision)
        or type(repository_revision) is not int
        or repository_revision < 1
    ):
        raise ValueError("invalid cached active selection")
    generation = cache_root / version / revision
    configs = generation / "configs"
    if generation.is_symlink() or configs.is_symlink():
        raise ValueError("configuration cache contains a symlink")
    files: dict[str, bytes] = {}
    identities: list[tuple[str, str, str, str]] = []
    for file in configs.rglob("*.yaml"):
        if file.is_symlink() or any(
            parent.is_symlink() for parent in file.parents if parent.is_relative_to(configs)
        ):
            raise ValueError("configuration cache contains a symlink")
        relative = file.relative_to(configs).as_posix()
        if relative.startswith("deployments/"):
            continue
        if not relative.startswith(("services/", "instances/", "templates/")):
            raise ValueError("unexpected cached configuration path")
        content = file.read_bytes()
        document = _published_yaml(content)
        identity = resolve_publication_identity(relative, document)
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        identities.append(
            (
                identity.config_type,
                identity.config_id,
                identity.file_version,
                hashlib.sha256(canonical).hexdigest(),
            )
        )
        files[relative] = content
    if not files or closure_sha256(deployment_id, service_id, identities) != revision:
        raise ValueError("cached active configuration digest mismatch")
    service_files = [
        name for name in files
        if name.startswith("services/")
        and resolve_publication_identity(
            name, _published_yaml(files[name])
        ).config_id == service_id
    ]
    if len(service_files) != 1:
        raise ValueError("cached active configuration has no unique Service")
    selected = SelectedBundle(revision, repository_revision, files)
    if os.environ.get("WIND_HUB_DEPLOYMENT_VERSION", "1.0.0") != version:
        raise ValueError("cached Deployment version differs from bootstrap selection")
    stage_selected_bundle(selected, cache_root, deployment_id, service_id)
    return configs / service_files[0], selected
