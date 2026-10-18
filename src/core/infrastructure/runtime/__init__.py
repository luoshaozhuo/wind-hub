"""进程级运行时设施（默认 executor 等）。"""

from .executor import configure_default_thread_pool

__all__ = ["configure_default_thread_pool"]
