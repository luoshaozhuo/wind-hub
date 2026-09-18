"""Builtin processors — 导入即触发自注册（见 processor_registry）。"""

from wind_hub.adapter.outbound.processor.builtin.deadband import DeadbandProcessor  # noqa: F401
from wind_hub.adapter.outbound.processor.builtin.quality_check import (  # noqa: F401
    QualityCheckProcessor,
)
from wind_hub.adapter.outbound.processor.builtin.unit_convert import (
    UnitConvertProcessor,  # noqa: F401
)

__all__ = ["UnitConvertProcessor", "DeadbandProcessor", "QualityCheckProcessor"]
