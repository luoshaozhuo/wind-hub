"""Wind Hub 跨进程共享的稳定异常体系。

本模块定义配置、协议、命令、Sink 和通用操作错误。Sink 失败经 RPC 与
HTTP API 构成跨进程错误契约（SINK_ERROR），故 SinkError 与其它共享异常
一样定义在 Core，由 Collector Sink 适配器抛出、Server Web API 映射。
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


class SinkError(WindHubError):
    """Sink 打开、写入、flush 或外部连接失败。"""


class OperationTimeoutError(WindHubError):
    """Wind Hub 显式操作超时；与 Python 内建 TimeoutError 区分。"""
