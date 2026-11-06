"""业务配置装配契约：独立于 Core 技术基础库及任一进程服务。"""

from .sink import Sink, SinkOutput
from .task import Task, devices_for_task, validate_task_references

__all__ = ["Sink", "SinkOutput", "Task", "devices_for_task", "validate_task_references"]
