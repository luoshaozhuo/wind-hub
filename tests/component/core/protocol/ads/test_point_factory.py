"""ads_point 地址工厂的构造校验测试。

显式 index 地址的 ADSPoint 由点表解析路径（parse_ads_point）与协议
调试入口共同使用；这里只覆盖工厂自身的归一化与校验规则。读写 API
只接受已配置 Point Key，动态点直传用例已随统一契约移除。
"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.infrastructure.protocol.ads import ads_point


class TestADSPointFactory:
    def test_defaults_size_from_type_width(self) -> None:
        point = ads_point("d", index_group=1, index_offset=2, data_type="uint16")
        assert point.data_type == "UINT"
        assert point.size == 2
        assert point.symbol is None
        assert point.address_resolved is True

    def test_rejects_bool_and_negative_index(self) -> None:
        with pytest.raises(ConfigError, match="index_group must be an integer"):
            ads_point("d", index_group=True, index_offset=0, data_type="DINT")
        with pytest.raises(ConfigError, match="index_offset must be >= 0"):
            ads_point("d", index_group=0, index_offset=-1, data_type="DINT")

    def test_rejects_unknown_type_and_size_mismatch(self) -> None:
        with pytest.raises(ConfigError, match="unsupported ADS type"):
            ads_point("d", index_group=0, index_offset=0, data_type="FLOAT128")
        with pytest.raises(ConfigError, match="does not match"):
            ads_point("d", index_group=0, index_offset=0, data_type="DINT", size=2)

    def test_string_allows_explicit_size(self) -> None:
        point = ads_point("d", index_group=0, index_offset=0, data_type="str", size=81)
        assert point.data_type == "STRING"
        assert point.size == 81
        with pytest.raises(ConfigError, match="size must be >= 0"):
            ads_point("d", index_group=0, index_offset=0, data_type="STRING", size=-1)
