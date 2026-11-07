"""跨模块共享的项目配置装配：与 Core 基础库和进程实现解耦。"""

from .diff import ConfigSnapshotDiff, diff_config_snapshots
from .effective import resolve_effective_device
from .shared_documents import SharedConfigPaths, SharedDocumentLoader, SharedDocuments
from .shared_snapshot import SharedDomainSnapshot, build_shared_snapshot
from .sink import Sink, SinkOutput
from .site_loader import load_domain
from .snapshot import ConfigSnapshot
from .task import Task, devices_for_task, validate_task_references
from .versioned_templates import TemplateRef, VersionedTemplateRepository

__all__ = [
    "ConfigSnapshot",
    "ConfigSnapshotDiff",
    "SharedConfigPaths",
    "SharedDocumentLoader",
    "SharedDocuments",
    "SharedDomainSnapshot",
    "Sink",
    "SinkOutput",
    "load_domain",
    "Task",
    "TemplateRef",
    "VersionedTemplateRepository",
    "build_shared_snapshot",
    "devices_for_task",
    "diff_config_snapshots",
    "resolve_effective_device",
    "validate_task_references",
]
