"""Wind Hub 跨进程共享的稳定异常体系。

本模块只定义配置、协议、命令和通用操作错误。具体进程专属外部系统错误
（例如 Collector SinkError）由对应组件定义，避免 Core 反向吸收运行时职责。
"""

from __future__ import annotations


class WindHubError(Exception):
    """Wind Hub 稳定语义异常基类。"""


class ConfigError(WindHubError):
    """配置缺失、格式非法或跨文件不一致。"""


class ProtocolError(WindHubError):
    """协议连接、读写、握手或链路级失败。"""


class CommandError(WindHubError):
    """设备命令请求错误。

    Args:
        message: 稳定可读错误说明。
        command_id: 关联原始命令的幂等标识；无关联命令时允许传空字符串。
    """

    def __init__(self, message: str, command_id: str) -> None:
        super().__init__(message)
        self.command_id = command_id


class OperationTimeoutError(WindHubError):
    """Wind Hub 显式操作超时；与 Python 内建 TimeoutError 区分。"""
