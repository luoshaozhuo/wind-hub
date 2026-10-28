"""原始地址和同址别名规则用例，后续由 Claude 统一执行。"""

import pytest

from seahorse.infrastructure.dc_ems_contract import (
    CONTROL_ADDRESSES,
    OriginalAddress,
    validate_original_addresses,
)

pytestmark = pytest.mark.unit


def test_control_offsets_are_original_modbus_locators() -> None:
    assert CONTROL_ADDRESSES["ctrlActPow"] == OriginalAddress(1, "holding", 41557, "F")
    assert CONTROL_ADDRESSES["ctrlStop"] == OriginalAddress(1, "coil", 12809, "B")


def test_incomplete_farm_mapping_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing DC-EMS mapping"):
        validate_original_addresses(dict(CONTROL_ADDRESSES))
