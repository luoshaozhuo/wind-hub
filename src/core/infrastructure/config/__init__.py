"""可复用的外部配置适配器。"""

from .adapter import YamlConfigAdapter
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic

__all__ = [
    "YamlConfigAdapter",
    "read_yaml_mapping",
    "write_yaml_mapping_atomic",
]
