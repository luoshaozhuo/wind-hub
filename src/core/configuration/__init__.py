"""Shared wind-hub configuration assembly built on Core primitives."""

from .digest import config_dir_digest
from .effective import resolve_effective_device
from .shared_documents import SharedConfigPaths, SharedDocumentLoader, SharedDocuments
from .shared_snapshot import SharedDomainSnapshot, build_shared_snapshot
from .versioned_templates import TemplateRef, VersionedTemplateRepository

__all__ = [
    "SharedConfigPaths",
    "SharedDocumentLoader",
    "SharedDocuments",
    "SharedDomainSnapshot",
    "TemplateRef",
    "VersionedTemplateRepository",
    "build_shared_snapshot",
    "config_dir_digest",
    "resolve_effective_device",
]
