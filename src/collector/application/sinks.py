"""兼容 Collector 旧导入路径；Sink 契约的唯一实现在 Core。"""

from core.application.sink_config import *  # noqa: F403
from core.application.sink_config import __all__ as __all__
