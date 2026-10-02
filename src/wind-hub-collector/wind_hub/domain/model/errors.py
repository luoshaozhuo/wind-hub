"""Wind Hub 领域层稳定异常体系。

所有需要跨 application/adapter 边界传播的语义错误继承 WindHubError；第三方库
异常应在 adapter 边界转换后再进入领域调用链。
"""

from __future__ import annotations


class WindHubError(Exception):
    """Wind Hub 领域异常基类。"""


class ConfigError(WindHubError):
    """配置缺失、格式非法或跨文件不一致。"""


class ProtocolError(WindHubError):
    """协议连接、读写、握手或链路级失败。"""


class SinkError(WindHubError):
    """Sink 打开、写入、flush 或外部连接失败。"""


class CommandError(WindHubError):
    """领域层命令错误。

    command_id 用于调用方关联原始 Command；未知设备/点等请求错误可使用该类型。
    """

    def __init__(self, message: str, command_id: str) -> None:
        super().__init__(message)
        self.command_id = command_id


class OperationTimeoutError(WindHubError):
    """Wind Hub 显式操作超时；与 Python 内建 TimeoutError 区分。"""
