"""可复用的外部配置适配器。"""

from .digest import config_dir_digest
from .shared_documents import SharedConfigPaths, SharedDocumentLoader, SharedDocuments
from .shared_snapshot import SharedDomainSnapshot, build_shared_snapshot
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic

__all__ = [
    "config_dir_digest",
    "SharedConfigPaths",
    "SharedDocumentLoader",
    "SharedDocuments",
    "SharedDomainSnapshot",
    "build_shared_snapshot",
    "read_yaml_mapping",
    "write_yaml_mapping_atomic",
]
