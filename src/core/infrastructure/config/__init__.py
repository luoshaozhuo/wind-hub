"""可复用的外部配置适配器。"""

from .adapter import YamlConfigAdapter, YamlConfigSnapshot
from .yaml import (
    fingerprint_config_files,
    fingerprint_config_set,
    fingerprint_config_topics,
    read_yaml_mapping,
    write_yaml_mapping_atomic,
)

__all__ = [
    "YamlConfigAdapter",
    "YamlConfigSnapshot",
    "fingerprint_config_files",
    "fingerprint_config_set",
    "fingerprint_config_topics",
    "read_yaml_mapping",
    "write_yaml_mapping_atomic",
]
