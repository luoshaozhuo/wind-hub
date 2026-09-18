"""Processor adapters — 导入内置处理器以触发自注册（见 processor_registry）。"""

from wind_hub.adapter.outbound.processor.builtin import (  # noqa: F401
    DeadbandProcessor,
    QualityCheckProcessor,
    UnitConvertProcessor,
)

__all__ = ["UnitConvertProcessor", "DeadbandProcessor", "QualityCheckProcessor"]
