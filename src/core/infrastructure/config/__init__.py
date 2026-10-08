"""可复用的外部配置适配器。"""

from .yaml import YamlConfigReader, fingerprint_config_set, read_yaml_mapping

__all__ = ["YamlConfigReader", "fingerprint_config_set", "read_yaml_mapping"]
