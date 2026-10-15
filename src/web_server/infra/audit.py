"""控制与配置变更的审计日志。

专用 logger ``web_server.audit``，记录谁在何时对什么资源做了什么以及结果
（经 Request-ID 关联请求上下文，由 ``RequestIdFilter`` 注入记录）。

调用方禁止传入明文密码、Token 或其他敏感凭据；点位写入值属过程数据，
默认只记录目标与结果，不记录值本身。
"""

from __future__ import annotations

import logging
from typing import Any

audit_logger = logging.getLogger("web_server.audit")


def audit(action: str, **fields: Any) -> None:
    """记录一次控制/配置变更事件。

    ``fields`` 以 key=value 形式输出；None 值省略。敏感字段（password、
    token、secret 等）会被防御性打码，但调用方本就不应传入。
    """
    parts = []
    for key, value in sorted(fields.items()):
        if value is None:
            continue
        if any(marker in key.lower() for marker in ("password", "token", "secret")):
            value = "***"
        parts.append(f"{key}={value!r}")
    audit_logger.info("action=%s %s", action, " ".join(parts))
