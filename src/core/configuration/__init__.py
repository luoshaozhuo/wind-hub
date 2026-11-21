"""Shared wind-hub configuration assembly built on Core primitives."""

from .deployment import (
    DeploymentSelection,
    VersionSelection,
    closure_sha256,
    parse_deployment,
    service_instance_refs,
)
from .digest import config_dir_digest
from .effective import resolve_effective_device
from .shared_documents import SharedConfigPaths, SharedDocumentLoader, SharedDocuments
from .shared_snapshot import SharedDomainSnapshot, build_shared_snapshot
from .versioned_templates import TemplateRef, VersionedTemplateRepository

__all__ = [
    "DeploymentSelection",
    "SharedConfigPaths",
    "SharedDocumentLoader",
    "SharedDocuments",
    "SharedDomainSnapshot",
    "TemplateRef",
    "VersionSelection",
    "VersionedTemplateRepository",
    "build_shared_snapshot",
    "closure_sha256",
    "config_dir_digest",
    "parse_deployment",
    "resolve_effective_device",
    "service_instance_refs",
]
