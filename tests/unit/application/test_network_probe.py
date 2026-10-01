"""network_probe 纯解析测试。"""

import pytest

from wind_hub.infra.network_probe import expand_network


def test_expand_network_limits_size() -> None:
    assert expand_network("192.168.1.0/30") == ["192.168.1.1", "192.168.1.2"]

    with pytest.raises(ValueError, match="limit"):
        expand_network("10.0.0.0/8")
