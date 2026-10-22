"""发布映射阻止两字寄存器交叠，避免覆盖其他测量点。"""
import pytest

from seahorse.infrastructure.modbus_publisher import PublishConfig, PublishPoint


def test_register_span_overlap() -> None:
    first = PublishPoint(
        device_id="a", point_id="p", host="127.0.0.1", port=15020,
        unit_id=1, address=100,
    )
    second = first.model_copy(update={"point_id": "q", "address": 101})
    with pytest.raises(ValueError, match="overlapping"):
        PublishConfig(points=[first, second])
