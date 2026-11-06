"""可复用的外部配置适配器。"""

from .digest import config_dir_digest
from .generic_yaml import (
    ConfigDiff,
    ConfigDiffEntry,
    ConfigErrorCode,
    ConfigOperationError,
    MISSING,
    compare_config,
    dump_yaml,
    parse_yaml,
    read_yaml,
    save_yaml,
)
from .shared_documents import SharedConfigPaths, SharedDocumentLoader, SharedDocuments
from .shared_snapshot import SharedDomainSnapshot, build_shared_snapshot
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic

__all__ = [
    "ConfigDiff",
    "ConfigDiffEntry",
    "ConfigErrorCode",
    "ConfigOperationError",
    "MISSING",
    "compare_config",
    "dump_yaml",
    "parse_yaml",
    "read_yaml",
    "save_yaml",
    "config_dir_digest",
    "SharedConfigPaths",
    "SharedDocumentLoader",
    "SharedDocuments",
    "SharedDomainSnapshot",
    "build_shared_snapshot",
    "read_yaml_mapping",
    "write_yaml_mapping_atomic",
]
